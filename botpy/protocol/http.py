import asyncio
import inspect
import json
import logging
from urllib.parse import urlsplit, urlunsplit
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Protocol

import httpx

from .errors import ApiError, TransportError


TRACE_ID_HEADER = "X-Tps-trace-Id"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
MAX_LOG_SUMMARY_CHARS = 1024
MAX_LOG_DEPTH = 4
MAX_LOG_ITEMS = 20
SENSITIVE_LOG_KEYS = {
    "access_token",
    "app_secret",
    "authorization",
    "bot_token",
    "client_secret",
    "content",
    "content_raw",
    "cookie",
    "credential",
    "email",
    "file_data",
    "file_image",
    "file_info",
    "group_openid",
    "group_openids",
    "member_openid",
    "openid",
    "openids",
    "phone",
    "presigned_url",
    "refresh_token",
    "secret",
    "send_message",
    "set_cookie",
    "sign",
    "signature",
    "token",
    "union_openid",
    "upload_id",
    "user_openid",
    "user_openids",
    "verify_message",
}
NORMALIZED_SENSITIVE_LOG_KEYS = {
    "".join(character for character in key.casefold() if character.isalnum()) for key in SENSITIVE_LOG_KEYS
}


def _safe_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _redact_log_value(value: Any, depth: int = 0) -> Any:
    if depth >= MAX_LOG_DEPTH:
        return "<max-depth>"
    if isinstance(value, Mapping):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_LOG_ITEMS:
                result["<truncated>"] = f"{len(value) - MAX_LOG_ITEMS} more fields"
                break
            normalized = "".join(character for character in str(key).casefold() if character.isalnum())
            result[str(key)] = (
                "<redacted>" if normalized in NORMALIZED_SENSITIVE_LOG_KEYS else _redact_log_value(item, depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        items = [_redact_log_value(item, depth + 1) for item in value[:MAX_LOG_ITEMS]]
        if len(value) > MAX_LOG_ITEMS:
            items.append(f"<{len(value) - MAX_LOG_ITEMS} more items>")
        return items
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{type(value).__name__} {len(value)} bytes>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return f"<{type(value).__name__}>"


def _summarize_payload(payload: Any) -> str:
    redacted = _redact_log_value(payload)
    try:
        summary = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        summary = f"<{type(redacted).__name__}>"
    if len(summary) <= MAX_LOG_SUMMARY_CHARS:
        return summary
    return summary[:MAX_LOG_SUMMARY_CHARS] + f"...<{len(summary) - MAX_LOG_SUMMARY_CHARS} chars truncated>"


def _request_was_not_sent(error: BaseException) -> bool:
    """Return whether a transport error happened before an HTTP request could be sent."""

    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return True
    if isinstance(error, RuntimeError):
        message = str(error).casefold()
        return message in {"session is closed", "http client is closed"} or "client has been closed" in message
    return False


class AccessTokenProvider(Protocol):
    app_id: str

    async def get_access_token(self, force_refresh: bool = False) -> str:
        ...


class ApiClient:
    """统一的异步 HTTP 客户端，负责认证、解析、重试和结构化错误。"""

    def __init__(
        self,
        token_provider: AccessTokenProvider,
        *,
        base_url: str = "https://api.sgroup.qq.com",
        timeout: float = 5,
        max_retries: int = 2,
        retry_base_delay: float = 0.5,
        user_agent: str = "qq-botpy",
        session: Optional[httpx.AsyncClient] = None,
        logger: Optional[logging.Logger] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        ssl: Any = None,
    ) -> None:
        self.token_provider = token_provider
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.retry_base_delay = max(0, retry_base_delay)
        self.user_agent = user_agent
        self._session = session
        self._owns_session = session is None
        self._session_lock = asyncio.Lock()
        self._token_refresh_lock = asyncio.Lock()
        self._logger = logger or logging.getLogger("botpy.protocol.http")
        self._sleep = sleep
        self.ssl = ssl
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_session and self._session and not self._session.is_closed:
            await self._session.close()

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> Any:
        return await self.request("PUT", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> Any:
        return await self.request("PATCH", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return await self.request("DELETE", path, **kwargs)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        auth: bool = True,
        retries: Optional[int] = None,
        retry_unsafe: bool = False,
        retry_ambiguous: bool = False,
        timeout: Optional[float] = None,
        before_attempt: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Any:
        self._ensure_open()
        method = method.upper()
        url = self._build_url(path)
        log_url = _safe_url(url)
        max_retries = self.max_retries if retries is None else max(0, retries)
        can_retry_status = method in SAFE_METHODS or retry_unsafe
        can_retry_transport = can_retry_status or retry_ambiguous
        request_headers: Dict[str, str] = dict(headers or {})
        request_headers.setdefault("User-Agent", self.user_agent)
        caller_supplied_authorization = "Authorization" in request_headers
        token_was_refreshed = False

        if auth:
            token = await self.token_provider.get_access_token()
            request_headers.setdefault("Authorization", f"QQBot {token}")
            app_id = getattr(self.token_provider, "app_id", None)
            if app_id:
                request_headers.setdefault("X-Union-Appid", app_id)

        last_error: Optional[BaseException] = None
        retry_count = 0
        request_attempts = 0

        while True:
            try:
                if before_attempt is not None:
                    await before_attempt()
                self._ensure_open()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if request_attempts:
                    raise TransportError(
                        f"HTTP {method} retry aborted before another request attempt",
                        method=method,
                        url=url,
                        cause=exc,
                        attempts=request_attempts,
                    ) from exc
                raise
            try:
                # A session can be closed between attempts during lifecycle changes. Re-check it
                # instead of pinning a stale object for the entire retry loop.
                session = await self._get_session()
                request_attempts += 1
                response = await session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    data=data,
                    files=files,
                    headers=request_headers,
                    timeout=httpx.Timeout(self.timeout if timeout is None else timeout),
                )
                payload = await self._read_response(response)
                trace_id = response.headers.get(TRACE_ID_HEADER)

                if 200 <= response.status_code < 300:
                    if self._logger.isEnabledFor(logging.DEBUG):
                        self._logger.debug(
                            "[botpy] HTTP %s %s -> %s trace_id=%s response=%s",
                            method,
                            log_url,
                            response.status_code,
                            trace_id,
                            _summarize_payload(payload),
                        )
                    return payload

                if (
                    response.status_code == 401
                    and auth
                    and not caller_supplied_authorization
                    and not token_was_refreshed
                ):
                    token = await self._refresh_access_token(token)
                    request_headers["Authorization"] = f"QQBot {token}"
                    token_was_refreshed = True
                    continue

                retry_after = self._parse_retry_after(response.headers, payload)
                error = ApiError.from_response(
                    status=response.status_code,
                    payload=payload,
                    trace_id=trace_id,
                    method=method,
                    url=url,
                    retry_after=retry_after,
                )
                retryable_status = response.status_code == 429 or response.status_code >= 500
                if retryable_status and can_retry_status and retry_count < max_retries:
                    await self._sleep(retry_after if retry_after is not None else self._delay(retry_count))
                    retry_count += 1
                    continue
                if self._logger.isEnabledFor(logging.DEBUG):
                    self._logger.debug(
                        "[botpy] HTTP %s %s -> %s trace_id=%s response=%s",
                        method,
                        log_url,
                        response.status_code,
                        trace_id,
                        _summarize_payload(payload),
                    )
                raise error
            except ApiError:
                raise
            except (asyncio.TimeoutError, httpx.HTTPError, RuntimeError) as exc:
                if isinstance(exc, RuntimeError) and not _request_was_not_sent(exc):
                    raise
                last_error = exc
                retryable = can_retry_transport or _request_was_not_sent(exc)
                if retryable and retry_count < max_retries:
                    self._logger.debug(
                        "[botpy] HTTP %s %s transport error on request attempt %s: %s; retrying",
                        method,
                        log_url,
                        request_attempts,
                        type(exc).__name__,
                    )
                    await self._sleep(self._delay(retry_count))
                    retry_count += 1
                    continue
                self._logger.warning(
                    "[botpy] HTTP %s %s transport error after %s attempt(s): %s",
                    method,
                    log_url,
                    request_attempts,
                    type(exc).__name__,
                )
                break

        raise TransportError(
            f"HTTP {method} request failed",
            method=method,
            url=url,
            cause=last_error,
            attempts=request_attempts,
        ) from last_error

    async def _get_session(self) -> httpx.AsyncClient:
        self._ensure_open()
        if self._session and not self._session.is_closed:
            return self._session
        async with self._session_lock:
            self._ensure_open()
            if not self._session or self._session.is_closed:
                self._session = httpx.AsyncClient(verify=self.ssl if self.ssl is not None else True)
                self._owns_session = True
        return self._session

    async def _refresh_access_token(self, stale_token: str) -> str:
        async with self._token_refresh_lock:
            current_token = await self.token_provider.get_access_token()
            if current_token != stale_token:
                return current_token
            compare_and_refresh = getattr(self.token_provider, "refresh_access_token", None)
            if callable(compare_and_refresh):
                return await compare_and_refresh(stale_token)
            return await self.token_provider.get_access_token(force_refresh=True)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("HTTP client is closed")

    def _build_url(self, path: str) -> str:
        if path.startswith("https://") or path.startswith("http://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _delay(self, attempt: int) -> float:
        return self.retry_base_delay * (2**attempt)

    @staticmethod
    async def _read_response(response: httpx.Response) -> Any:
        if response.status_code == 204:
            return None
        # ``httpx.Response.text`` is a property, while older test doubles may
        # expose an awaitable ``text()`` method.  Accept
        # both forms so custom transports remain source-compatible.
        text = response.text
        if inspect.isawaitable(text):
            text = await text
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    @staticmethod
    def _parse_retry_after(headers: Mapping[str, str], payload: Any) -> Optional[float]:
        raw_value: Any = headers.get("Retry-After")
        if raw_value is None and isinstance(payload, Mapping):
            raw_value = payload.get("retry_after")
        try:
            return max(0.0, float(raw_value))
        except (TypeError, ValueError):
            return None
