from typing import Any, Mapping, Optional


class BotPyError(RuntimeError):
    """所有新版 botpy 异常的基类。"""


class ApiError(BotPyError):
    """QQ OpenAPI 返回的结构化错误。"""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[int] = None,
        trace_id: Optional[str] = None,
        method: Optional[str] = None,
        url: Optional[str] = None,
        response: Any = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.trace_id = trace_id
        self.method = method
        self.url = url
        self.response = response
        self.retry_after = retry_after

    def __str__(self) -> str:
        context = []
        if self.status is not None:
            context.append(f"status={self.status}")
        if self.code is not None:
            context.append(f"code={self.code}")
        if self.trace_id:
            context.append(f"trace_id={self.trace_id}")
        suffix = f" ({', '.join(context)})" if context else ""
        return f"{self.message}{suffix}"

    @classmethod
    def from_response(
        cls,
        *,
        status: int,
        payload: Any,
        trace_id: Optional[str],
        method: str,
        url: str,
        retry_after: Optional[float] = None,
    ) -> "ApiError":
        message = str(payload)
        code = None
        if isinstance(payload, Mapping):
            raw_message = payload.get("message") or payload.get("msg")
            if raw_message is not None:
                message = str(raw_message)
            raw_code = payload.get("code")
            if isinstance(raw_code, int):
                code = raw_code
            elif isinstance(raw_code, str) and raw_code.lstrip("-").isdigit():
                code = int(raw_code)

        error_type = RateLimitError if status == 429 else cls
        if status in (401, 403):
            error_type = AuthenticationError
        return error_type(
            message,
            status=status,
            code=code,
            trace_id=trace_id,
            method=method,
            url=url,
            response=payload,
            retry_after=retry_after,
        )


class AuthenticationError(ApiError):
    """认证信息无效或权限不足。"""


class RateLimitError(ApiError):
    """请求被平台限流。"""


class TransportError(BotPyError):
    """网络连接、超时或传输层错误。"""

    def __init__(
        self,
        message: str,
        *,
        method: Optional[str] = None,
        url: Optional[str] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.url = url
        self.cause = cause


class UploadDailyLimitExceededError(BotPyError):
    """分片上传准备阶段触发平台每日上传额度限制。"""

    def __init__(self, file_path: str, file_size: int, message: str) -> None:
        super().__init__(message)
        self.file_path = file_path
        self.file_size = file_size
