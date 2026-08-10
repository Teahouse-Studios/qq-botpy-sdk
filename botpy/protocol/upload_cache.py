import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Union

from .message import MediaFileType
from .models import ChatScope


DEFAULT_UPLOAD_CACHE_SIZE = 500
DEFAULT_UPLOAD_CACHE_SAFETY_MARGIN = 60


def compute_file_hash(data: bytes) -> str:
    """计算上传缓存使用的 MD5 内容摘要。"""

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    return hashlib.md5(data).hexdigest()


@dataclass(frozen=True)
class UploadCacheStats:
    size: int
    max_size: int


@dataclass(frozen=True)
class _UploadCacheEntry:
    file_info: str
    file_uuid: str
    expires_at: float


class UploadCache:
    """按内容、目标和媒体类型缓存平台返回的 ``file_info``。"""

    def __init__(
        self,
        *,
        max_size: int = DEFAULT_UPLOAD_CACHE_SIZE,
        safety_margin: int = DEFAULT_UPLOAD_CACHE_SAFETY_MARGIN,
        clock: Callable[[], float] = time.time,
        logger: Any = None,
    ) -> None:
        if not isinstance(max_size, int) or isinstance(max_size, bool) or max_size <= 0:
            raise ValueError("max_size must be a positive integer")
        if not isinstance(safety_margin, int) or isinstance(safety_margin, bool) or safety_margin < 0:
            raise ValueError("safety_margin must be a non-negative integer")
        self.max_size = max_size
        self.safety_margin = safety_margin
        self._clock = clock
        self._logger = logger
        self._entries: "OrderedDict[str, _UploadCacheEntry]" = OrderedDict()

    @staticmethod
    def compute_hash(data: bytes) -> str:
        return compute_file_hash(data)

    def get(
        self,
        content_hash: str,
        scope: ChatScope,
        target_id: str,
        file_type: Union[int, MediaFileType],
    ) -> Optional[str]:
        key = self._key(content_hash, scope, target_id, file_type)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        if self._logger is not None:
            self._logger.debug("[botpy] upload cache hit uuid=%s", entry.file_uuid)
        return entry.file_info

    def set(
        self,
        content_hash: str,
        scope: ChatScope,
        target_id: str,
        file_type: Union[int, MediaFileType],
        file_info: str,
        file_uuid: str,
        ttl: Union[int, float],
    ) -> None:
        if not isinstance(file_info, str) or not file_info:
            return
        try:
            ttl_seconds = float(ttl)
        except (TypeError, ValueError):
            return
        if ttl_seconds <= 0:
            return

        self._evict_expired()
        while len(self._entries) >= self.max_size:
            self._entries.popitem(last=False)

        # Never keep a cached value beyond the server-reported TTL, even when
        # the TTL itself is shorter than the normal safety margin.
        effective_ttl = max(
            ttl_seconds - self.safety_margin,
            min(10.0, ttl_seconds * 0.9),
        )
        key = self._key(content_hash, scope, target_id, file_type)
        self._entries[key] = _UploadCacheEntry(
            file_info=file_info,
            file_uuid=file_uuid if isinstance(file_uuid, str) else "",
            expires_at=self._clock() + effective_ttl,
        )
        self._entries.move_to_end(key)

    def store_response(
        self,
        content_hash: str,
        scope: ChatScope,
        target_id: str,
        file_type: Union[int, MediaFileType],
        response: Mapping[str, Any],
    ) -> None:
        self.set(
            content_hash,
            scope,
            target_id,
            file_type,
            response.get("file_info"),
            response.get("file_uuid", ""),
            response.get("ttl", 0),
        )

    def stats(self) -> UploadCacheStats:
        self._evict_expired()
        return UploadCacheStats(size=len(self._entries), max_size=self.max_size)

    def clear(self) -> None:
        self._entries.clear()

    def _evict_expired(self) -> None:
        now = self._clock()
        expired = [key for key, entry in self._entries.items() if now >= entry.expires_at]
        for key in expired:
            self._entries.pop(key, None)

    @staticmethod
    def _key(
        content_hash: str,
        scope: ChatScope,
        target_id: str,
        file_type: Union[int, MediaFileType],
    ) -> str:
        if not isinstance(content_hash, str) or not content_hash:
            raise ValueError("content_hash is required")
        if scope not in ("c2c", "group", "channel", "dm"):
            raise ValueError("scope is invalid")
        if not isinstance(target_id, str) or not target_id:
            raise ValueError("target_id is required")
        normalized_type = MediaFileType(file_type)
        return f"{content_hash}:{scope}:{target_id}:{int(normalized_type)}"
