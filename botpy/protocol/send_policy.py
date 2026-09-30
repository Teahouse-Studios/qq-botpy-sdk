# -*- coding: utf-8 -*-
"""消息发送的失败分类与重发策略。

QQ 开放平台的消息发送接口把业务错误放在响应体的 ``err_code`` 里（文档明确要求
「根据 ``err_code`` 判断请求是否失败」，而不是依赖 HTTP 状态码或 ``message``）。
本模块把这些错误码分成四类，并给出对应的处理动作：

``PASSIVE_FALLBACK_CODES``
    被动回复的上下文已经不可用（``msg_id`` / ``event_id`` 过期、越权、事件不支持
    回复等）。此时去掉 ``msg_id`` / ``event_id`` / ``msg_seq``，回退成主动消息重发。
``DUPLICATE_MESSAGE_CODES``
    消息被去重。首次发送时换一个 ``msg_seq`` 重发一次；仍然被去重则直接抛错
    （此时消息可能已经发出但 id 不可知，不能继续重试）。
``TRANSIENT_CODES``
    平台明确提示「请重试 / 请稍后重试」或频控类错误，按指数退避重发。
``UNREACHABLE_CODES``
    已知不可达或需要人工处理的确定性失败（不是群成员、被禁言、无好友关系、
    用户拒收、内容违规、参数错误等），立即抛错，绝不重试。

错误码取值与描述来自 ``generated_docs/api-v2`` 中
``v2_groups_group_openid_messages.post`` / ``v2_users_user_openid_messages.post``
两个接口的「错误码」表，可用 ``generated_docs/extract_send_errors.py`` 复核覆盖率。

发送总时长受 ``total_timeout``（默认 60 秒）约束：一旦下一次退避会超出预算，或单次
尝试已经让总时长越界，就停止重发并抛出 :class:`MessageSendTimeoutError`。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Mapping, MutableMapping, Optional

import httpx

from .errors import ApiError, TransportError, extract_error_code

__all__ = [
    "SendErrorCategory",
    "MessageSendPolicy",
    "MessageSendTimeoutError",
    "PASSIVE_FALLBACK_CODES",
    "DUPLICATE_MESSAGE_CODES",
    "TRANSIENT_CODES",
    "UNREACHABLE_CODES",
    "ACCEPTED_ERR_CODES",
    "DOCUMENTED_MESSAGE_CODES",
    "classify_send_error",
    "DEFAULT_TOTAL_TIMEOUT",
    "DEFAULT_BACKOFF_BASE",
]

DEFAULT_TOTAL_TIMEOUT = 60.0
DEFAULT_BACKOFF_BASE = 3.0

#: 真正的网络层失败（超时、连接中断、协议错误、代理错误）；``UnsupportedProtocol``
#: 这类配置错误不在其中，重发它只会白白等待。
_NETWORK_CAUSE_TYPES = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.ProtocolError,
    httpx.ProxyError,
)

#: 被动回复上下文失效：去掉 msg_id/event_id/msg_seq，回退成主动消息重发。
#: 前半部分是 v2 群聊/单聊接口的错误码，后半部分是频道消息（openapi）接口里
#: 语义相同的旧错误码。
PASSIVE_FALLBACK_CODES: Mapping[int, str] = {
    304103: "消息ID已过期，不能回复",
    304026: "MSG_ID 回复的消息 id 错误",
    304027: "MSG_EXPIRE 回复的消息过期",
    304028: "MSG_PROTECT 非 At 当前用户的消息不允许回复",
    40034005: "回复消息msg_id已过期",
    40034024: "请求参数msg_id无效或越权",
    40034025: "请求参数event_id无效",
    40034026: "请求参数event_id已过期",
    40034027: "该事件不支持回复消息",
    40034128: "被动回复时间或次数超限",
}

#: 消息被去重：首次发送换一个 msg_seq 重发一次，仍失败直接抛错。
DUPLICATE_MESSAGE_CODES: Mapping[int, str] = {
    40054005: "消息被去重",
}

#: 平台明确提示「请重试」，或属于短时服务异常（转存 / 上传 / 下载 / 查询 / 语料），
#: 按指数退避重发。语义不明确的错误码一律不进这张表，避免重复投递。
TRANSIENT_CODES: Mapping[int, str] = {
    304007: "GET_GUILD 查询频道异常",
    304008: "GET_BOT 查询机器人异常",
    304009: "GET_CHENNAL 查询子频道异常",
    304010: "CHANGE_IMAGE_URL 图片转存错误",
    304017: "UPLOAD_IMAGE 图片上传错误",
    304021: "GET_FILE 下载文件错误",
    304029: "CORPUS_ERROR 调语料服务错误",
    304052: "发消息设置引导超频",
    40034004: "富媒体信息转存失败（请重试）",
    40034100: "主动消息发送超过频控限制（请降低发送频率或等待配额恢复）",
    40054006: "验证好友关系失败（请重试）",
    50055001: "消息发送异常（请稍后重试）",
    50055002: "消息发送异常（请稍后重试）",
    50055006: "ARK消息发送异常（请稍后重试）",
}

#: 已知不可达 / 需要人工处理，重试没有意义，立即抛错。
UNREACHABLE_CODES: Mapping[int, str] = {
    22006: "消息类型与内容不匹配",
    50059: "输入类型错误",
    304003: "URL_NOT_ALLOWED url 未报备",
    304004: "无权限使用该ARK模板",
    304005: "EMBED_LIMIT embed 长度超限",
    304006: "SERVER_CONFIG 后台配置错误",
    304011: "NO_TEMPLATE 模板不存在",
    304012: "GET_TEMPLATE 取模板错误",
    304014: "TEMPLATE_PRIVILEGE 没有模板权限",
    304016: "SEND_ERROR 发消息错误",
    304018: "SESSION_NOT_EXIST 机器人没连上 gateway",
    304019: "AT_EVERYONE_TIMES @全体成员 次数超限",
    304020: "FILE_SIZE 文件大小超限",
    304022: "PUSH_TIME 推送消息时间限制",
    304025: "BEAT 消息被打击",
    304030: "CORPUS_NOT_MATCH 语料不匹配",
    304031: "私信已关闭",
    304032: "私信不存在",
    304033: "拉私信错误",
    304034: "不是私信成员",
    304035: "推送消息超过子频道数量限制",
    304036: "无Markdown模板权限",
    304037: "没有发消息按钮组件的权限",
    304038: "消息按钮组件不存在",
    304039: "消息按钮组件解析错误",
    304040: "消息按钮组件消息内容错误",
    304044: "取消息设置错误",
    304045: "子频道主动消息数限频（配额类，退避无法恢复）",
    304046: "不允许在此子频道发主动消息",
    304047: "主动消息推送超过限制的子频道数",
    304048: "不允许在此频道发主动消息",
    304049: "私信主动消息数限频（配额类，退避无法恢复）",
    304050: "私信主动消息总量限频（配额类，退避无法恢复）",
    304051: "消息设置引导请求构造错误",
    304061: "消息内容无效",
    304062: "订阅按钮数量达到上限",
    304064: "订阅消息未授权",
    304080: "文件信息无效",
    305007: "键盘样式参数错误",
    340067: "获取机器人信息失败",
    340069: "消息类型无效",
    40034006: "消息内容违规",
    40034008: "markdown参数有空值",
    40034009: "markdown参数有换行符",
    40034010: "模版参数中不能含有markdown语法",
    40034011: "无效的markdown内容",
    40034029: "内联键盘行/列超限",
    40034101: "机器人非群成员",
    40034105: "主动消息发送失败，无权限",
    40034106: "消息不支持该指令类型",
    40034108: "指令参数长度超限",
    40034109: "指令参数解析失败",
    40034122: "召回消息已达区间上限",
    40034123: "不支持召回消息",
    40034124: "markdown消息参数错误",
    40034127: "无markdown模板权限",
    40054002: "机器人被禁言",
    40054003: "机器人不是群成员",
    40054004: "无好友关系",
    40054007: "消息长度超限",
    40054010: "不允许发送URL",
    40054013: "用户拒收消息",
    40054016: "机器人已下线",
    40054018: "消息过长或异常",
}

#: 201/202 异步受理成功时响应体里同样带 err_code，这些取值必须当作成功。
ACCEPTED_ERR_CODES = frozenset({304023, 304024})

#: 文档中出现过的全部消息发送错误码（含上表未分类的保留值），用于覆盖率核对。
DOCUMENTED_MESSAGE_CODES = frozenset(
    set(PASSIVE_FALLBACK_CODES) | set(DUPLICATE_MESSAGE_CODES) | set(TRANSIENT_CODES) | set(UNREACHABLE_CODES)
)


class SendErrorCategory:
    """错误码分类常量。"""

    PASSIVE_FALLBACK = "passive_fallback"
    DUPLICATE = "duplicate"
    TRANSIENT = "transient"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


def classify_send_error(code: Optional[int]) -> str:
    """把平台错误码映射到处理动作；未知错误码按「不可重试」处理。"""

    if code is None:
        return SendErrorCategory.UNKNOWN
    if code in DUPLICATE_MESSAGE_CODES:
        return SendErrorCategory.DUPLICATE
    if code in PASSIVE_FALLBACK_CODES:
        return SendErrorCategory.PASSIVE_FALLBACK
    if code in TRANSIENT_CODES:
        return SendErrorCategory.TRANSIENT
    if code in UNREACHABLE_CODES:
        return SendErrorCategory.UNREACHABLE
    return SendErrorCategory.UNKNOWN


def describe_send_error(code: Optional[int]) -> str:
    """返回错误码的文档描述，用于拼接更可读的异常信息。"""

    if code is None:
        return ""
    for table in (DUPLICATE_MESSAGE_CODES, PASSIVE_FALLBACK_CODES, TRANSIENT_CODES, UNREACHABLE_CODES):
        if code in table:
            return table[code]
    return ""


class MessageSendTimeoutError(TransportError):
    """消息发送在总时长预算内未能完成。"""

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        elapsed: float,
        timeout: float,
        cause: Optional[BaseException] = None,
        last_code: Optional[int] = None,
    ) -> None:
        super().__init__(message, cause=cause, attempts=attempts)
        self.elapsed = elapsed
        self.timeout = timeout
        self.last_code = last_code

    def __str__(self) -> str:
        return f"{self.message} (attempts={self.attempts}, elapsed={self.elapsed:.1f}s, timeout={self.timeout:g}s)"


class MessageSendPolicy:
    """按平台错误码决定「回退主动 / 换 seq / 退避重发 / 直接抛错」。

    Args:
      total_timeout: 单次逻辑发送的总时长预算，默认 60 秒。
      backoff_base: 指数退避基数，默认 3 秒；第 n 次重发等待 ``base * 2 ** (n-1)``。
      max_attempts: 可选的硬性尝试次数上限，``None`` 表示只受总时长约束。
      clock / sleep: 便于测试注入。
    """

    def __init__(
        self,
        *,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        max_attempts: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if total_timeout <= 0:
            raise ValueError("total_timeout must be positive")
        if backoff_base <= 0:
            raise ValueError("backoff_base must be positive")
        if max_attempts is not None and (isinstance(max_attempts, bool) or max_attempts <= 0):
            raise ValueError("max_attempts must be a positive integer or None")
        self.total_timeout = float(total_timeout)
        self.backoff_base = float(backoff_base)
        self.max_attempts = max_attempts
        self._clock = clock
        self._sleep = sleep
        self._logger = logger or logging.getLogger("botpy.protocol.send_policy")

    def backoff_delay(self, retry_index: int) -> float:
        """第 ``retry_index``（从 0 开始）次重发前的等待秒数：3、6、12、24……"""

        if retry_index < 0:
            raise ValueError("retry_index must not be negative")
        return self.backoff_base * (2**retry_index)

    async def execute(
        self,
        payload: MutableMapping[str, Any],
        attempt: Callable[[MutableMapping[str, Any]], Awaitable[Any]],
        *,
        next_sequence: Optional[Callable[[MutableMapping[str, Any]], Optional[int]]] = None,
    ) -> Any:
        """执行一次逻辑发送，按需改写 ``payload`` 并重发。

        ``payload`` 会被原地修改：回退主动消息时删除 ``msg_id`` / ``event_id`` /
        ``msg_seq``，去重重发时替换 ``msg_seq``，调用方可以直接把最终 payload
        当作实际发出的内容记录下来。

        Args:
          payload: 本次消息的请求体。
          attempt: 发送一次，返回平台响应；失败时抛出 :class:`ApiError` 或
            :class:`TransportError`。
          next_sequence: 可选回调，返回一个新的 ``msg_seq``；返回 ``None``
            表示该场景无法改 seq，此时去重错误直接抛错。
        """

        started = self._clock()
        deadline = started + self.total_timeout
        attempts = 0
        retry_index = 0
        sequence_bumped = False
        last_error: Optional[BaseException] = None
        last_code: Optional[int] = None

        while True:
            attempts += 1
            try:
                result = await attempt(payload)
                code = self._business_error_code(result)
                if code is None:
                    if self._clock() > deadline:
                        self._logger.warning(
                            "[botpy] 消息发送耗时 %.1f 秒，超过 %.0f 秒预算，但平台已受理本次发送",
                            self._clock() - started,
                            self.total_timeout,
                        )
                    return result
                # 2xx 响应体里带着业务错误码：按同一条流水线处理。
                last_error = ApiError(
                    describe_send_error(code) or "消息发送失败",
                    code=code,
                    response=result,
                )
                last_code = code
                category = classify_send_error(code)
            except ApiError as error:
                last_error = error
                last_code = error.code
                category = classify_send_error(error.code)
                if category is SendErrorCategory.UNKNOWN and self._is_rate_limited(error):
                    # HTTP 429 通常不带 err_code，但语义上和频控类错误一致。
                    category = SendErrorCategory.TRANSIENT
            except (asyncio.TimeoutError, httpx.HTTPError) as error:
                # 直接冒出的网络层异常，没有经过 SDK 包装。``InvalidURL`` /
                # ``UnsupportedProtocol`` 这类配置错误不在重试范围内。
                last_error = error
                last_code = None
                if isinstance(error, asyncio.TimeoutError) or isinstance(error, _NETWORK_CAUSE_TYPES):
                    category = SendErrorCategory.TRANSIENT
                else:
                    category = None
            except TransportError as error:
                last_error = error
                last_code = None
                category = SendErrorCategory.TRANSIENT if self._is_wrapped_network_failure(error) else None

            if category is None:
                # 不是网络原因（例如 Gateway 未恢复、客户端已关闭），交给调用方处理。
                raise last_error

            if category is SendErrorCategory.PASSIVE_FALLBACK:
                if payload.get("msg_id") or payload.get("event_id"):
                    self._logger.info(
                        "[botpy] 被动回复上下文已失效(err_code=%s: %s)，回退为主动消息重发",
                        last_code,
                        describe_send_error(last_code),
                    )
                    payload.pop("msg_id", None)
                    payload.pop("event_id", None)
                    payload.pop("msg_seq", None)
                    continue
                # 已经是主动消息，没有可回退的上下文。
                raise last_error

            if category is SendErrorCategory.DUPLICATE:
                if attempts == 1 and not sequence_bumped and payload.get("msg_id") and next_sequence is not None:
                    sequence = next_sequence(payload)
                    if sequence is not None:
                        self._logger.info(
                            "[botpy] 消息被去重(err_code=%s)，换 msg_seq=%s 重发一次",
                            last_code,
                            sequence,
                        )
                        payload["msg_seq"] = sequence
                        sequence_bumped = True
                        continue
                self._logger.warning(
                    "[botpy] 消息被去重(err_code=%s) 且无法再换 seq，消息可能已发出但 id 不可知",
                    last_code,
                )
                raise last_error

            if category is not SendErrorCategory.TRANSIENT:
                raise last_error

            if self.max_attempts is not None and attempts >= self.max_attempts:
                raise self._timeout_error(last_error, last_code, attempts, started, deadline)
            await self._backoff(retry_index, attempts, started, deadline, last_error, last_code)
            retry_index += 1

    # -- 内部实现 ---------------------------------------------------------- #
    @staticmethod
    def _business_error_code(result: Any) -> Optional[int]:
        """2xx 响应体里非 0 的 ``err_code`` 表示业务失败。"""

        code = extract_error_code(result)
        if code is None or code == 0 or code in ACCEPTED_ERR_CODES:
            return None
        return code

    @staticmethod
    def _is_rate_limited(error: ApiError) -> bool:
        return error.status == 429

    @staticmethod
    def _is_wrapped_network_failure(error: TransportError) -> bool:
        """判断被 SDK 包装过的 :class:`TransportError` 是否源于真实的网络失败。

        ``botpy`` 也用 ``TransportError`` 表达自身状态：Gateway 未就绪（``attempts=0``，
        消息根本没发出）、重试被中止（``cause`` 是另一个 ``TransportError``）等。
        这些重发没有意义，只有底层 ``cause`` 确实是 httpx 网络/超时异常、
        且至少发出过一次请求时才退避重发。
        """

        if error.attempts == 0:
            return False
        return isinstance(error.cause, _NETWORK_CAUSE_TYPES)

    async def _backoff(
        self,
        retry_index: int,
        attempts: int,
        started: float,
        deadline: float,
        last_error: BaseException,
        last_code: Optional[int],
    ) -> None:
        delay = self.backoff_delay(retry_index)
        now = self._clock()
        if now >= deadline or now + delay > deadline:
            raise self._timeout_error(last_error, last_code, attempts, started, deadline)
        self._logger.info(
            "[botpy] 消息发送失败(%s)，%.0f 秒后重发（第 %s 次尝试）",
            self._describe_failure(last_code, last_error),
            delay,
            attempts + 1,
        )
        await self._sleep(delay)

    def _timeout_error(
        self,
        last_error: Optional[BaseException],
        last_code: Optional[int],
        attempts: int,
        started: float,
        deadline: float,
    ) -> MessageSendTimeoutError:
        elapsed = max(0.0, self._clock() - started)
        detail = self._describe_failure(last_code, last_error)
        return MessageSendTimeoutError(
            f"消息发送在 {self.total_timeout:g} 秒内未成功（最后一次失败：{detail}）",
            attempts=attempts,
            elapsed=elapsed,
            timeout=self.total_timeout,
            cause=last_error,
            last_code=last_code,
        )

    @staticmethod
    def _describe_failure(last_code: Optional[int], last_error: Optional[BaseException]) -> str:
        if last_code is not None:
            description = describe_send_error(last_code)
            return f"err_code={last_code} {description}".strip()
        if isinstance(last_error, ApiError) and last_error.status is not None:
            return f"HTTP {last_error.status}"
        if last_error is None:
            return "未知错误"
        return type(last_error).__name__
