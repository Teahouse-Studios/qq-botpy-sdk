import asyncio
import json
import logging
import time
from typing import Any, Optional

import aiohttp

from .errors import ApiError, AuthenticationError, TransportError


_log = logging.getLogger("botpy.protocol.auth")


class TokenManager:
    """并发安全的 QQ Bot access token 管理器。"""

    def __init__(
        self,
        app_id: str,
        secret: str,
        *,
        base_url: str = "https://bots.qq.com",
        timeout: float = 20,
        refresh_margin: float = 60,
        max_retries: int = 2,
        session: Optional[aiohttp.ClientSession] = None,
        logger: Optional[logging.Logger] = None,
        user_agent: str = "qq-botpy",
        ssl: Any = None,
    ) -> None:
        if not app_id:
            raise ValueError("app_id is required")
        if not secret:
            raise ValueError("secret is required")

        self.app_id = app_id.strip()
        self.secret = secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.refresh_margin = max(0, refresh_margin)
        self.max_retries = max(0, max_retries)
        self._session = session
        self._owns_session = session is None
        self._session_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._access_token: Optional[str] = None
        self._expires_at = 0.0
        self._refresh_at = 0.0
        self._logger = logger or _log
        self.user_agent = user_agent
        self.ssl = ssl
        self._background_task: Optional[asyncio.Task] = None

    @property
    def cached_token(self) -> Optional[str]:
        return self._access_token

    @property
    def expires_at(self) -> float:
        return self._expires_at

    @property
    def status(self) -> str:
        if self._refresh_lock.locked():
            return "refreshing"
        if self._is_valid():
            return "valid"
        return "expired" if self._access_token else "none"

    def clear(self) -> None:
        self._access_token = None
        self._expires_at = 0.0
        self._refresh_at = 0.0

    def set_cached_token(self, token: Optional[str], expires_at: float = 0) -> None:
        self._access_token = token
        if not token:
            self._expires_at = 0.0
            self._refresh_at = 0.0
            return

        now = time.time()
        self._expires_at = float(expires_at)
        self._refresh_at = max(now, self._expires_at - self.refresh_margin)

    async def get_access_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._is_valid():
            return self._access_token  # type: ignore[return-value]

        # Lock + double-check provides single-flight semantics for concurrent callers.
        async with self._refresh_lock:
            if not force_refresh and self._is_valid():
                return self._access_token  # type: ignore[return-value]
            return await self._fetch_token()

    async def close(self) -> None:
        await self.stop_background_refresh()
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    def start_background_refresh(self) -> asyncio.Task:
        """启动单实例 token 提前刷新循环。"""

        if self._background_task is None or self._background_task.done():
            self._background_task = asyncio.create_task(
                self._background_refresh_loop(),
                name=f"[botpy] token-refresh-{self.app_id}",
            )
        return self._background_task

    async def stop_background_refresh(self) -> None:
        task = self._background_task
        self._background_task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _is_valid(self) -> bool:
        return bool(self._access_token) and time.time() < self._refresh_at

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        async with self._session_lock:
            if not self._session or self._session.closed:
                self._session = aiohttp.ClientSession()
                self._owns_session = True
        return self._session

    async def _fetch_token(self) -> str:
        session = await self._get_session()
        url = f"{self.base_url}/app/getAppAccessToken"
        last_error: Optional[BaseException] = None

        for attempt in range(self.max_retries + 1):
            try:
                async with session.post(
                    url,
                    json={"appId": self.app_id, "clientSecret": self.secret},
                    headers={"User-Agent": self.user_agent},
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ssl=self.ssl,
                ) as response:
                    text = await response.text()
                    try:
                        data: Any = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        data = text

                    trace_id = response.headers.get("X-Tps-trace-Id")
                    if not 200 <= response.status < 300:
                        error = ApiError.from_response(
                            status=response.status,
                            payload=data,
                            trace_id=trace_id,
                            method="POST",
                            url=url,
                        )
                        if response.status >= 500 and attempt < self.max_retries:
                            await asyncio.sleep(0.5 * (2**attempt))
                            continue
                        raise AuthenticationError(
                            error.message,
                            status=error.status,
                            code=error.code,
                            trace_id=error.trace_id,
                            method=error.method,
                            url=error.url,
                            response=error.response,
                        )

                    if not isinstance(data, dict):
                        raise AuthenticationError("token endpoint returned a non-object response", response=data)

                    token = data.get("access_token")
                    expires_in = data.get("expires_in")
                    try:
                        expires_seconds = float(expires_in)
                    except (TypeError, ValueError):
                        expires_seconds = 0
                    if not isinstance(token, str) or not token or expires_seconds <= 0:
                        raise AuthenticationError("token response is missing access_token or expires_in", response=data)

                    now = time.time()
                    refresh_margin = min(self.refresh_margin, max(1.0, expires_seconds * 0.1))
                    self._access_token = token
                    self._expires_at = now + expires_seconds
                    self._refresh_at = self._expires_at - refresh_margin
                    self._logger.debug(
                        "[botpy] access token refreshed, expires_in=%s seconds",
                        int(expires_seconds),
                    )
                    return token
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                break

        raise TransportError(
            "failed to refresh access token",
            method="POST",
            url=url,
            cause=last_error,
        ) from last_error

    async def _background_refresh_loop(self) -> None:
        while True:
            try:
                await self.get_access_token()
                delay = max(1.0, self._refresh_at - time.time())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning("[botpy] background token refresh failed: %s", exc)
                delay = 30.0
            await asyncio.sleep(delay)
