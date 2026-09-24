# -*- coding: utf-8 -*-
import asyncio
from typing import Any, Awaitable, Callable, Optional, ClassVar, Dict
from urllib.parse import quote

from . import logging
from .errors import HttpErrorDict, ServerError
from .protocol.errors import ApiError
from .protocol.http import ApiClient, _safe_url
from .robot import Token
from .types import robot

_log = logging.get_logger()


class Route:
    DOMAIN: ClassVar[str] = "api.sgroup.qq.com"
    SANDBOX_DOMAIN: ClassVar[str] = "sandbox.api.sgroup.qq.com"
    SCHEME: ClassVar[str] = "https"

    def __init__(self, method: str, path: str, is_sandbox: str = False, **parameters: Any) -> None:
        if not isinstance(method, str) or not method or not method.isascii() or not method.isalpha():
            raise ValueError("HTTP method must contain ASCII letters only")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("route path must start with '/'")
        if any(ord(character) < 32 or ord(character) == 127 for character in path):
            raise ValueError("route path must not contain control characters")
        self.method: str = method.upper()
        self.path: str = path
        self.is_sandbox = is_sandbox
        self.parameters = parameters

    @property
    def formatted_path(self) -> str:
        """Return the route with path parameters safely percent-encoded."""

        encoded = {key: quote(str(value), safe="") for key, value in self.parameters.items()}
        return self.path.format_map(encoded)

    @property
    def url(self):
        if self.is_sandbox:
            d = self.SANDBOX_DOMAIN
        else:
            d = self.DOMAIN
        return "{}://{}{}".format(self.SCHEME, d, self.formatted_path)


class BotHttp:
    """
    TODO 增加请求重试功能 @veehou
    TODO 增加并发请求的锁控制 @veehou
    """

    def __init__(
        self,
        timeout: int,
        is_sandbox: bool = False,
        app_id: str = None,
        secret: str = None,
        base_url: Optional[str] = None,
        token_base_url: str = "https://bots.qq.com",
        user_agent: str = "qq-botpy",
        ssl: Any = None,
    ):
        self.timeout = timeout
        self.is_sandbox = is_sandbox
        domain = Route.SANDBOX_DOMAIN if is_sandbox else Route.DOMAIN
        self.base_url = (base_url or f"{Route.SCHEME}://{domain}").rstrip("/")
        self.token_base_url = token_base_url.rstrip("/")
        self.user_agent = user_agent
        self.ssl = ssl

        self._token: Optional[Token] = (
            None
            if not app_id
            else Token(
                app_id=app_id,
                secret=secret,
                base_url=self.token_base_url,
                user_agent=self.user_agent,
                ssl=self.ssl,
            )
        )
        self._session = None
        self._client: Optional[ApiClient] = None
        self._global_over: Optional[asyncio.Event] = None
        self._headers: Optional[dict] = None
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._client:
            await self._client.close()
        if self._token:
            await self._token.close()
        self._session = None
        self._client = None

    async def check_session(self):
        if self._closed:
            raise RuntimeError("[botpy] HTTP 客户端已关闭")
        if self._token is None:
            raise RuntimeError("[botpy] token 尚未初始化")
        await self._token.check_token()
        self._headers = {
            "Authorization": self._token.get_string(),
            "X-Union-Appid": self._token.app_id,
        }

        if self._client is None:
            self._client = ApiClient(
                self._token,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=2,
                user_agent=self.user_agent,
                ssl=self.ssl,
                logger=_log,
            )

    async def request(self, route: Route, retry_time: int = 0, **kwargs: Any):
        if retry_time > 2:
            raise RuntimeError("[botpy] 请求重试次数超过限制")
        # some checking if it's a JSON request
        if "json" in kwargs:
            json_ = kwargs["json"]
            json__get = json_.get("file_image")
            if json__get and isinstance(json__get, bytes):
                form_fields = {}
                for key, value in kwargs.pop("json").items():
                    if not value:
                        continue
                    if isinstance(value, dict):
                        if key == "message_reference":
                            _log.error(
                                f"[botpy] 接口参数传入异常, 请求连接: {_safe_url(route.url)}, "
                                f"错误原因: file_image与message_reference不能同时传入，"
                                f"备注: sdk已按照优先级，去除message_reference参数"
                            )
                        continue
                    # httpx's multipart encoder accepts text/bytes payloads;
                    # stringify scalar fields so numeric flags retain the
                    # previous form-data behaviour instead of raising a
                    # serialization error.
                    form_fields[key] = (None, value if isinstance(value, (str, bytes, bytearray)) else str(value))
                form_fields["file_image"] = (None, json__get)
                kwargs["files"] = form_fields

        await self.check_session()
        route.is_sandbox = self.is_sandbox
        _log.debug("[botpy] 请求方式: %s, 请求url: %s", route.method, _safe_url(route.url))

        json_body = kwargs.pop("json", None)
        data = kwargs.pop("data", None)
        files = kwargs.pop("files", None)
        params = kwargs.pop("params", None)
        timeout = kwargs.pop("timeout", None)
        retry_unsafe = kwargs.pop("retry_unsafe", False)
        retry_ambiguous = kwargs.pop("retry_ambiguous", False)
        before_attempt: Optional[Callable[[], Awaitable[None]]] = kwargs.pop("before_attempt", None)
        if kwargs:
            raise TypeError("不支持的 HTTP 请求参数: %s" % ", ".join(sorted(kwargs)))

        try:
            return await self._client.request(
                route.method,
                route.formatted_path,
                params=params,
                json_body=json_body,
                data=data,
                files=files,
                retries=2 - retry_time,
                retry_unsafe=retry_unsafe,
                retry_ambiguous=retry_ambiguous,
                timeout=timeout,
                before_attempt=before_attempt,
            )
        except ApiError as error:
            exception_type = HttpErrorDict.get(error.status, ServerError)
            raise exception_type(
                msg=error.message,
                status=error.status,
                code=error.code,
                trace_id=error.trace_id,
                method=error.method,
                url=error.url,
                response=error.response,
                retry_after=error.retry_after,
            ) from error

    async def request_url(
        self,
        method: str,
        url: str,
        *,
        data: Any = None,
        headers: Optional[Dict[str, str]] = None,
        auth: bool = False,
        retries: int = 2,
        timeout: Optional[float] = None,
    ):
        """请求完整 URL，主要用于无机器人鉴权的媒体预签名地址。"""

        await self.check_session()
        try:
            return await self._client.request(
                method,
                url,
                data=data,
                headers=headers,
                auth=auth,
                retries=retries,
                retry_unsafe=False,
                timeout=timeout,
            )
        except ApiError as error:
            exception_type = HttpErrorDict.get(error.status, ServerError)
            raise exception_type(
                msg=error.message,
                status=error.status,
                code=error.code,
                trace_id=error.trace_id,
                method=error.method,
                url=error.url,
                response=error.response,
                retry_after=error.retry_after,
            ) from error

    async def get_access_token(self, force_refresh: bool = False) -> str:
        if self._closed:
            raise RuntimeError("[botpy] HTTP 客户端已关闭")
        if self._token is None:
            raise RuntimeError("[botpy] token 尚未初始化")
        return await self._token.get_access_token(force_refresh=force_refresh)

    async def login(self, token: Token) -> robot.Robot:
        """login后保存token和session"""

        if self._closed:
            raise RuntimeError("[botpy] HTTP 客户端已关闭")

        previous_token = self._token
        if self._client and self._client.token_provider is not token:
            await self._client.close()
            self._client = None
        if previous_token and previous_token is not token:
            await previous_token.close()
        self._token = token
        await self.check_session()
        token.start_background_refresh()
        self._global_over = asyncio.Event()
        self._global_over.set()

        data = await self.request(Route("GET", "/users/@me"))
        # TODO 检查机器人token错误的raise exception @veehou
        return data
