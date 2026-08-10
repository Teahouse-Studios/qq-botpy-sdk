# -*- coding: utf-8 -*-
import asyncio
from json.decoder import JSONDecodeError
from typing import Any, Optional, ClassVar, Union, Dict

import aiohttp
from aiohttp import ClientResponse, FormData, multipart, hdrs, payload

from . import logging
from .errors import HttpErrorDict, ServerError
from .protocol.errors import ApiError
from .protocol.http import ApiClient
from .robot import Token
from .types import robot

X_TPS_TRACE_ID = "X-Tps-trace-Id"

_log = logging.get_logger()

# 请求成功的返回码
HTTP_OK_STATUS = [200, 202, 204]


class _FormData(FormData):
    def _gen_form_data(self) -> multipart.MultipartWriter:
        """Encode a list of fields using the multipart/form-data MIME format"""
        if getattr(self, "_is_processed", False):
            return self._writer  # rewrite this part of FormData object to enable retry of request
        for dispparams, headers, value in self._fields:
            try:
                if hdrs.CONTENT_TYPE in headers:
                    part = payload.get_payload(
                        value,
                        content_type=headers[hdrs.CONTENT_TYPE],
                        headers=headers,
                        encoding=self._charset,
                    )
                else:
                    part = payload.get_payload(
                        value,
                        headers=headers,
                        encoding=self._charset,
                    )
            except Exception as exc:
                print(value)
                raise TypeError(
                    "Can not serialize value type: %r\n " "headers: %r\n value: %r" % (type(value), headers, value)
                ) from exc

            if dispparams:
                part.set_content_disposition(
                    "form-data",
                    quote_fields=self._quote_fields,
                    **dispparams,
                )
                assert part.headers is not None
                part.headers.popall(hdrs.CONTENT_LENGTH, None)

            self._writer.append_payload(part)

        self._is_processed = True
        return self._writer


async def _handle_response(response: ClientResponse) -> Union[Dict[str, Any], str]:
    url = response.request_info.url
    try:
        condition = response.headers["content-type"] == "application/json"
        # note that when content-type is application/json, aiohttp will directly auto-sub encoding to be utf-8
        data = await response.json() if condition else await response.text()
    except (KeyError, JSONDecodeError):
        data = None
    if response.status in HTTP_OK_STATUS:
        _log.debug(f"[botpy] 请求成功, 请求连接: {url}, 返回内容: {data}, trace_id:{response.headers.get(X_TPS_TRACE_ID)}")
        return data
    else:
        _log.error(
            f"[botpy] 接口请求异常，请求连接: {url}, "
            f"错误代码: {response.status}, 返回内容: {data}, trace_id:{response.headers.get(X_TPS_TRACE_ID)}"
            # trace_id 用于定位接口问题
        )
        error_dict_get = HttpErrorDict.get(response.status)
        # type of data should be dict or str or None, so there should be a condition to check and prevent bug
        message = data["message"] if isinstance(data, dict) else str(data)
        if not error_dict_get:
            raise ServerError(message) from None  # adding from None to prevent chain exception being raised
        raise error_dict_get(msg=message) from None


class Route:
    DOMAIN: ClassVar[str] = "api.sgroup.qq.com"
    SANDBOX_DOMAIN: ClassVar[str] = "sandbox.api.sgroup.qq.com"
    SCHEME: ClassVar[str] = "https"

    def __init__(self, method: str, path: str, is_sandbox: str = False, **parameters: Any) -> None:
        self.method: str = method
        self.path: str = path
        self.is_sandbox = is_sandbox
        self.parameters = parameters

    @property
    def url(self):
        if self.is_sandbox:
            d = self.SANDBOX_DOMAIN
        else:
            d = self.DOMAIN
        _url = "{}://{}{}".format(self.SCHEME, d, self.path)

        # path的参数:
        if self.parameters:
            _url = _url.format_map(self.parameters)
        return _url


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
        self._session: Optional[aiohttp.ClientSession] = None
        self._client: Optional[ApiClient] = None
        self._global_over: Optional[asyncio.Event] = None
        self._headers: Optional[dict] = None

    async def close(self) -> None:
        if self._client:
            await self._client.close()
        if self._token:
            await self._token.close()
        self._session = None
        self._client = None

    async def check_session(self):
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
                kwargs["data"] = _FormData()
                for k, v in kwargs.pop("json").items():
                    if v:
                        if isinstance(v, dict):
                            if k == "message_reference":
                                _log.error(
                                    f"[botpy] 接口参数传入异常, 请求连接: {route.url}, "
                                    f"错误原因: file_image与message_reference不能同时传入，"
                                    f"备注: sdk已按照优先级，去除message_reference参数"
                                )
                        else:
                            kwargs["data"].add_field(k, v)

        await self.check_session()
        route.is_sandbox = self.is_sandbox
        _log.debug("[botpy] 请求方式: %s, 请求url: %s", route.method, route.url)

        json_body = kwargs.pop("json", None)
        data = kwargs.pop("data", None)
        params = kwargs.pop("params", None)
        timeout = kwargs.pop("timeout", None)
        retry_unsafe = kwargs.pop("retry_unsafe", False)
        if kwargs:
            raise TypeError("不支持的 HTTP 请求参数: %s" % ", ".join(sorted(kwargs)))

        try:
            return await self._client.request(
                route.method,
                route.path.format_map(route.parameters),
                params=params,
                json_body=json_body,
                data=data,
                retries=2 - retry_time,
                retry_unsafe=retry_unsafe,
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
        if self._token is None:
            raise RuntimeError("[botpy] token 尚未初始化")
        return await self._token.get_access_token(force_refresh=force_refresh)

    async def login(self, token: Token) -> robot.Robot:
        """login后保存token和session"""

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
