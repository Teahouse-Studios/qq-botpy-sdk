import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Literal, Optional


ReplyFallbackReason = Literal["expired", "limit_exceeded"]


@dataclass(frozen=True)
class ReplyLimitResult:
    allowed: bool
    remaining: int
    should_fallback_to_proactive: bool
    fallback_reason: Optional[ReplyFallbackReason] = None
    message: Optional[str] = None


@dataclass
class _ReplyRecord:
    count: int
    first_reply_at: float


class ReplyLimiter:
    """跟踪单条入站消息的被动回复次数和有效期。"""

    def __init__(
        self,
        *,
        limit: int = 4,
        ttl_seconds: float = 3600,
        max_tracked_messages: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if not isinstance(max_tracked_messages, int) or max_tracked_messages <= 0:
            raise ValueError("max_tracked_messages must be a positive integer")
        self.limit = limit
        self.ttl_seconds = float(ttl_seconds)
        self.max_tracked_messages = max_tracked_messages
        self._clock = clock
        self._records: "OrderedDict[str, _ReplyRecord]" = OrderedDict()

    def check(self, message_id: str) -> ReplyLimitResult:
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id is required")
        now = self._clock()
        record = self._records.get(message_id)
        if record is None:
            return ReplyLimitResult(True, self.limit, False)
        self._records.move_to_end(message_id)

        if now - record.first_reply_at > self.ttl_seconds:
            return ReplyLimitResult(
                False,
                0,
                True,
                "expired",
                "passive reply window has expired; use a proactive message",
            )
        remaining = self.limit - record.count
        if remaining <= 0:
            return ReplyLimitResult(
                False,
                0,
                True,
                "limit_exceeded",
                f"passive reply limit reached ({self.limit}); use a proactive message",
            )
        return ReplyLimitResult(True, remaining, False)

    def record(self, message_id: str) -> None:
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id is required")
        now = self._clock()
        record = self._records.get(message_id)
        if record is None or now - record.first_reply_at > self.ttl_seconds:
            self._records[message_id] = _ReplyRecord(1, now)
        else:
            record.count += 1
        self._records.move_to_end(message_id)
        self._evict(now)

    def stats(self) -> dict:
        return {
            "tracked_messages": len(self._records),
            "total_replies": sum(record.count for record in self._records.values()),
        }

    def clear(self) -> None:
        self._records.clear()

    def _evict(self, now: float) -> None:
        expired = [
            message_id
            for message_id, record in self._records.items()
            if now - record.first_reply_at > self.ttl_seconds
        ]
        for message_id in expired:
            self._records.pop(message_id, None)
        while len(self._records) > self.max_tracked_messages:
            self._records.popitem(last=False)
