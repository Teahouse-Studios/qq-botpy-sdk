import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Protocol

import aiohttp

from .errors import ApiError, TransportError


TRACE_ID_HEADER = "X-Tps-trace-Id"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}


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
        session: Optional[aiohttp.ClientSession] = None,
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
        self._logger = logger or logging.getLogger("botpy.protocol.http")
        self._sleep = sleep
        self.ssl = ssl

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
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
        headers: Optional[Mapping[str, str]] = None,
        auth: bool = True,
        retries: Optional[int] = None,
        retry_unsafe: bool = False,
        timeout: Optional[float] = None,
    ) -> Any:
        method = method.upper()
        url = self._build_url(path)
        max_retries = self.max_retries if retries is None else max(0, retries)
        can_retry = method in SAFE_METHODS or retry_unsafe
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

        session = await self._get_session()
        last_error: Optional[BaseException] = None
        attempt = 0

        while True:
            try:
                async with session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    data=data,
                    headers=request_headers,
                    timeout=aiohttp.ClientTimeout(total=timeout or self.timeout),
                    ssl=self.ssl,
                ) as response:
                    payload = await self._read_response(response)
                    trace_id = response.headers.get(TRACE_ID_HEADER)

                    if 200 <= response.status < 300:
                        self._logger.debug(
                            "[botpy] HTTP %s %s -> %s trace_id=%s",
                            method,
                            url,
                            response.status,
                            trace_id,
                        )
                        return payload

                    if (
                        response.status == 401
                        and auth
                        and not caller_supplied_authorization
                        and not token_was_refreshed
                    ):
                        token = await self.token_provider.get_access_token(force_refresh=True)
                        request_headers["Authorization"] = f"QQBot {token}"
                        token_was_refreshed = True
                        continue

                    retry_after = self._parse_retry_after(response.headers, payload)
                    error = ApiError.from_response(
                        status=response.status,
                        payload=payload,
                        trace_id=trace_id,
                        method=method,
                        url=url,
                        retry_after=retry_after,
                    )
                    retryable_status = response.status == 429 or response.status >= 500
                    if retryable_status and can_retry and attempt < max_retries:
                        await self._sleep(retry_after if retry_after is not None else self._delay(attempt))
                        attempt += 1
                        continue
                    raise error
            except ApiError:
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
                if can_retry and attempt < max_retries:
                    await self._sleep(self._delay(attempt))
                    attempt += 1
                    continue
                break

        raise TransportError(
            f"HTTP {method} request failed",
            method=method,
            url=url,
            cause=last_error,
        ) from last_error

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        async with self._session_lock:
            if not self._session or self._session.closed:
                self._session = aiohttp.ClientSession()
                self._owns_session = True
        return self._session

    def _build_url(self, path: str) -> str:
        if path.startswith("https://") or path.startswith("http://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _delay(self, attempt: int) -> float:
        return self.retry_base_delay * (2**attempt)

    @staticmethod
    async def _read_response(response: aiohttp.ClientResponse) -> Any:
        if response.status == 204:
            return None
        text = await response.text()
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
