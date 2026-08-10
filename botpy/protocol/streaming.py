import asyncio
import time
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol

from .. import logging
from .errors import ApiError


DEFAULT_STREAM_THROTTLE_MS = 500
MIN_STREAM_THROTTLE_MS = 300
MAX_STREAM_RETRIES = 3
STREAM_RETRY_BASE_DELAY = 1.0


class StreamMessageApi(Protocol):
    async def post_c2c_stream_message(self, openid: str, **payload: Any) -> Any: ...


class StreamSession:
    """管理一条 C2C replace-mode 流式消息。

    ``update()`` 接收截至当前的完整文本，而不是增量片段。所有帧共享同一个
    ``msg_seq``，仅 ``index`` 递增；``complete()`` 会发送最终 DONE 帧。
    """

    def __init__(
        self,
        api: StreamMessageApi,
        *,
        openid: str,
        msg_id: str,
        event_id: str,
        msg_seq: int,
        throttle_ms: int = DEFAULT_STREAM_THROTTLE_MS,
        logger: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not openid:
            raise ValueError("openid is required")
        if not msg_id:
            raise ValueError("msg_id is required")
        if not event_id:
            raise ValueError("event_id is required")
        if not isinstance(msg_seq, int) or isinstance(msg_seq, bool):
            raise TypeError("msg_seq must be an integer")
        if not isinstance(throttle_ms, int) or isinstance(throttle_ms, bool):
            raise TypeError("throttle_ms must be an integer")

        self.api = api
        self.openid = openid
        self.msg_id = msg_id
        self.event_id = event_id
        self.msg_seq = msg_seq
        self.throttle_seconds = max(throttle_ms, MIN_STREAM_THROTTLE_MS) / 1000
        self.logger = logger or logging.get_logger()
        self._sleep = sleep
        self._clock = clock

        self._stream_msg_id: Optional[str] = None
        self._index = 0
        self._last_flush_at = 0.0
        self._last_sent_text = ""
        self._pending_text = ""
        self._timer_task: Optional[asyncio.Task] = None
        self._flush_lock = asyncio.Lock()
        self._completed = False

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def stream_message_id(self) -> Optional[str]:
        return self._stream_msg_id

    async def update(self, full_text: str) -> None:
        """更新完整文本，并保证发送频率不高于平台建议值。"""

        if self._completed:
            return
        if not isinstance(full_text, str):
            raise TypeError("full_text must be a string")

        self._pending_text = full_text
        if self._flush_lock.locked():
            return

        elapsed = self._clock() - self._last_flush_at
        if elapsed >= self.throttle_seconds:
            await self._flush(done=False)
            return

        if self._timer_task is None or self._timer_task.done():
            delay = max(0.0, self.throttle_seconds - elapsed)
            self._timer_task = asyncio.create_task(self._flush_after(delay))

    async def complete(self) -> Any:
        """发送携带最新文本的最终 DONE 帧。重复调用不会重复发送。"""

        if self._completed:
            return None
        self._completed = True
        self._cancel_timer()
        return await self._flush(done=True)

    def cancel(self) -> None:
        """取消会话，不发送 DONE 帧。"""

        self._completed = True
        self._cancel_timer()

    async def _flush_after(self, delay: float) -> None:
        try:
            await self._sleep(delay)
            if not self._completed:
                await self._flush(done=False)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.logger.error("[botpy] 流式消息节流发送失败: %s", exc)
        finally:
            if self._timer_task is asyncio.current_task():
                self._timer_task = None

    async def _flush(self, *, done: bool) -> Any:
        async with self._flush_lock:
            result = None
            while True:
                text = self._pending_text
                if not done and text == self._last_sent_text:
                    return result

                payload = {
                    "input_mode": "replace",
                    "input_state": 10 if done else 1,
                    "content_type": "markdown",
                    "content_raw": text,
                    "event_id": self.event_id,
                    "msg_id": self.msg_id,
                    "msg_seq": self.msg_seq,
                    "index": self._next_index(),
                }
                if self._stream_msg_id:
                    payload["stream_msg_id"] = self._stream_msg_id

                result = await self._send_with_retry(payload)
                if isinstance(result, Mapping) and result.get("id") and not self._stream_msg_id:
                    self._stream_msg_id = str(result["id"])
                self._last_sent_text = text
                self._last_flush_at = self._clock()

                if done or self._completed or self._pending_text == text:
                    return result

    async def _send_with_retry(self, payload: dict) -> Any:
        for attempt in range(MAX_STREAM_RETRIES + 1):
            try:
                return await self.api.post_c2c_stream_message(self.openid, **payload)
            except Exception as exc:
                if not self._is_rate_limit_error(exc) or attempt >= MAX_STREAM_RETRIES:
                    raise
                retry_after = getattr(exc, "retry_after", None)
                delay = (
                    float(retry_after)
                    if isinstance(retry_after, (int, float)) and retry_after > 0
                    else STREAM_RETRY_BASE_DELAY * (2**attempt)
                )
                self.logger.debug(
                    "[botpy] 流式消息被限流，%.1f 秒后进行第 %d/%d 次重试",
                    delay,
                    attempt + 1,
                    MAX_STREAM_RETRIES,
                )
                await self._sleep(delay)
                payload["index"] = self._next_index()
        return None

    def _next_index(self) -> int:
        current = self._index
        self._index += 1
        return current

    def _cancel_timer(self) -> None:
        task = self._timer_task
        self._timer_task = None
        if task is not None and not task.done():
            task.cancel()

    @staticmethod
    def _is_rate_limit_error(exc: BaseException) -> bool:
        if isinstance(exc, ApiError):
            if exc.status == 429 or exc.code in (429, 50002):
                return True
            if isinstance(exc.response, Mapping):
                response_code = exc.response.get("err_code", exc.response.get("code"))
                if response_code in (429, 50002, "429", "50002"):
                    return True
        code = getattr(exc, "code", None)
        if code in (429, 50002):
            return True
        message = str(exc).lower()
        return "rate limit" in message or "rate-limit" in message or "限流" in message
