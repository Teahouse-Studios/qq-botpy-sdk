# -*- coding: utf-8 -*-
"""向后兼容的异常名称。

新版代码使用 :mod:`botpy.protocol.errors` 中的结构化异常；旧异常名称继续保留，
以免破坏现有用户的 ``except AuthenticationFailedError`` 等写法。
"""

from typing import Any, Optional

from .protocol.errors import ApiError, BotPyError, RateLimitError, TransportError, UploadDailyLimitExceededError


class _LegacyApiError(ApiError):
    default_status: Optional[int] = None

    def __init__(
        self,
        msg: str,
        *,
        status: Optional[int] = None,
        code: Optional[int] = None,
        trace_id: Optional[str] = None,
        method: Optional[str] = None,
        url: Optional[str] = None,
        response: Any = None,
        retry_after: Optional[float] = None,
    ) -> None:
        self.msgs = msg
        super().__init__(
            msg,
            status=self.default_status if status is None else status,
            code=code,
            trace_id=trace_id,
            method=method,
            url=url,
            response=response,
            retry_after=retry_after,
        )

    def __str__(self) -> str:
        return self.msgs


class AuthenticationFailedError(_LegacyApiError):
    default_status = 401


class NotFoundError(_LegacyApiError):
    default_status = 404


class MethodNotAllowedError(_LegacyApiError):
    default_status = 405


class SequenceNumberError(RateLimitError):
    """旧版 429 异常名。新代码建议捕获 RateLimitError。"""

    def __init__(
        self,
        msg: str,
        *,
        status: Optional[int] = 429,
        code: Optional[int] = None,
        trace_id: Optional[str] = None,
        method: Optional[str] = None,
        url: Optional[str] = None,
        response: Any = None,
        retry_after: Optional[float] = None,
    ) -> None:
        self.msgs = msg
        super().__init__(
            msg,
            status=status,
            code=code,
            trace_id=trace_id,
            method=method,
            url=url,
            response=response,
            retry_after=retry_after,
        )

    def __str__(self) -> str:
        return self.msgs


class ServerError(_LegacyApiError):
    default_status = 500


class ForbiddenError(_LegacyApiError):
    default_status = 403


HttpErrorDict = {
    401: AuthenticationFailedError,
    403: ForbiddenError,
    404: NotFoundError,
    405: MethodNotAllowedError,
    429: SequenceNumberError,
    500: ServerError,
    502: ServerError,
    503: ServerError,
    504: ServerError,
}


__all__ = (
    "ApiError",
    "AuthenticationFailedError",
    "BotPyError",
    "ForbiddenError",
    "HttpErrorDict",
    "MethodNotAllowedError",
    "NotFoundError",
    "RateLimitError",
    "SequenceNumberError",
    "ServerError",
    "TransportError",
    "UploadDailyLimitExceededError",
)
