from dataclasses import dataclass
import time
from typing import Callable, Optional, Sequence


RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
RATE_LIMIT_DELAY = 60.0


@dataclass(frozen=True)
class CloseAction:
    should_reconnect: bool
    clear_session: bool = False
    refresh_token: bool = False
    fatal: bool = False
    reconnect_delay: Optional[float] = None
    reason: str = ""


class ReconnectPolicy:
    """Gateway 关闭码分类与跨连接重试退避状态。"""

    AUTH_FAILED = 4004
    INVALID_SESSION = 4006
    SEQ_OUT_OF_RANGE = 4007
    RATE_LIMITED = 4008
    SESSION_TIMEOUT = 4009
    INSUFFICIENT_INTENTS = 4914
    DISALLOWED_INTENTS = 4915

    def __init__(
        self,
        *,
        delays: Sequence[float] = RECONNECT_DELAYS,
        max_attempts: int = 100,
        quick_disconnect_threshold: float = 5.0,
        max_quick_disconnects: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not delays:
            raise ValueError("delays must not be empty")
        self.delays = tuple(max(0.0, float(delay)) for delay in delays)
        self.max_attempts = max(1, max_attempts)
        self.quick_disconnect_threshold = max(0.0, quick_disconnect_threshold)
        self.max_quick_disconnects = max(1, max_quick_disconnects)
        self._clock = clock
        self._attempts = 0
        self._connected_at: Optional[float] = None
        self._quick_disconnects = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    def on_connected(self) -> None:
        self._attempts = 0
        self._connected_at = self._clock()

    def next_delay(self, custom_delay: Optional[float] = None) -> Optional[float]:
        if self._attempts >= self.max_attempts:
            return None
        if custom_delay is None:
            index = min(self._attempts, len(self.delays) - 1)
            delay = self.delays[index]
        else:
            delay = max(0.0, float(custom_delay))
        self._attempts += 1
        return delay

    def handle_close(self, code: Optional[int], *, closing: bool = False) -> CloseAction:
        if closing:
            return CloseAction(False, reason="client closing")

        if code in (self.INSUFFICIENT_INTENTS, self.DISALLOWED_INTENTS):
            reason = "insufficient intents" if code == self.INSUFFICIENT_INTENTS else "disallowed intents"
            return CloseAction(False, fatal=True, reason=reason)

        if code == self.AUTH_FAILED:
            return CloseAction(True, refresh_token=True, reason="invalid token")

        if code == self.RATE_LIMITED:
            return CloseAction(True, reconnect_delay=RATE_LIMIT_DELAY, reason="rate limited")

        if code in (self.INVALID_SESSION, self.SEQ_OUT_OF_RANGE, self.SESSION_TIMEOUT, 9001, 9005):
            return CloseAction(
                True,
                clear_session=True,
                refresh_token=True,
                reason="invalid or expired session",
            )

        if code is not None and 4900 <= code <= 4913:
            return CloseAction(
                True,
                clear_session=True,
                refresh_token=True,
                reason="gateway internal error",
            )

        if code == 1000:
            return CloseAction(False, reason="normal closure")

        if self._is_quick_disconnect():
            self._quick_disconnects += 1
            if self._quick_disconnects >= self.max_quick_disconnects:
                self._quick_disconnects = 0
                return CloseAction(
                    True,
                    reconnect_delay=RATE_LIMIT_DELAY,
                    reason="too many quick disconnects",
                )
        else:
            self._quick_disconnects = 0

        return CloseAction(True, reason=f"close code {code}")

    def _is_quick_disconnect(self) -> bool:
        if self._connected_at is None:
            return False
        return self._clock() - self._connected_at < self.quick_disconnect_threshold
