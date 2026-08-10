import asyncio
import base64
from dataclasses import asdict
import inspect
from typing import Any, Optional

from ..middleware.conversation import HistoryEntry, RefEntry
from ..protocol.models import InboundAttachment, SessionState
from .kv import KVStore


class KVHistoryStore:
    """将 HistoryStore 映射到任意 KVStore。"""

    def __init__(
        self,
        store: KVStore,
        *,
        prefix: str = "botpy:history:",
        ttl: Optional[float] = None,
    ) -> None:
        self.store = store
        self.prefix = prefix
        self.ttl = ttl
        self._lock = asyncio.Lock()

    async def append(self, group_key: str, entry: HistoryEntry, limit: int) -> None:
        if limit < 1:
            raise ValueError("history limit must be positive")
        key = self._key(group_key)
        async with self._lock:
            raw = await _maybe_await(self.store.get(key))
            entries = _history_entries(raw)
            if any(existing.message_id == entry.message_id for existing in entries):
                return
            entries.append(entry)
            entries = entries[-limit:]
            await _maybe_await(
                self.store.set(key, [asdict(item) for item in entries], ttl=self.ttl)
            )

    async def list(self, group_key: str, limit: int) -> list[HistoryEntry]:
        if limit < 1:
            raise ValueError("history limit must be positive")
        raw = await _maybe_await(self.store.get(self._key(group_key)))
        return _history_entries(raw)[-limit:]

    async def clear(self, group_key: str) -> None:
        await _maybe_await(self.store.delete(self._key(group_key)))

    def _key(self, value: str) -> str:
        return self.prefix + _encode_key(value)


class KVRefIndexStore:
    """将 RefIndexStore 映射到任意 KVStore。"""

    def __init__(
        self,
        store: KVStore,
        *,
        prefix: str = "botpy:ref:",
        ttl: Optional[float] = 7 * 24 * 60 * 60,
    ) -> None:
        self.store = store
        self.prefix = prefix
        self.ttl = ttl

    async def get(self, key: str) -> Optional[RefEntry]:
        raw = await _maybe_await(self.store.get(self._key(key)))
        return _ref_entry(raw)

    async def set(self, key: str, entry: RefEntry) -> None:
        payload = asdict(entry)
        payload["attachments"] = [_attachment_payload(item) for item in entry.attachments]
        await _maybe_await(self.store.set(self._key(key), payload, ttl=self.ttl))

    async def delete(self, key: str) -> bool:
        return bool(await _maybe_await(self.store.delete(self._key(key))))

    def _key(self, value: str) -> str:
        return self.prefix + _encode_key(value)


class KVSessionStore:
    """使用 KVStore 实现 Gateway SessionStore。"""

    def __init__(
        self,
        store: KVStore,
        *,
        prefix: str = "botpy:session:",
        ttl: float = 300,
    ) -> None:
        self.store = store
        self.prefix = prefix
        self.ttl = ttl

    async def load(self, app_id: str, shard_id: int) -> Optional[SessionState]:
        raw = await _maybe_await(self.store.get(self._key(app_id, shard_id)))
        if not isinstance(raw, dict):
            return None
        try:
            state = SessionState(
                session_id=raw["session_id"],
                sequence=raw["sequence"],
                shard_id=raw["shard_id"],
                shard_count=raw["shard_count"],
            )
        except (KeyError, TypeError):
            await self.clear(app_id, shard_id)
            return None
        if (
            not isinstance(state.session_id, str)
            or not state.session_id
            or not isinstance(state.sequence, int)
            or isinstance(state.sequence, bool)
            or not isinstance(state.shard_id, int)
            or isinstance(state.shard_id, bool)
            or state.shard_id != shard_id
            or not isinstance(state.shard_count, int)
            or isinstance(state.shard_count, bool)
            or state.shard_count < 1
        ):
            await self.clear(app_id, shard_id)
            return None
        return state

    async def save(self, app_id: str, state: SessionState) -> None:
        if not state.session_id or state.sequence is None:
            return
        await _maybe_await(
            self.store.set(self._key(app_id, state.shard_id), asdict(state), ttl=self.ttl)
        )

    async def clear(self, app_id: str, shard_id: int) -> None:
        await _maybe_await(self.store.delete(self._key(app_id, shard_id)))

    async def close(self) -> None:
        flush = getattr(self.store, "flush", None)
        if callable(flush):
            await _maybe_await(flush())

    def _key(self, app_id: str, shard_id: int) -> str:
        return f"{self.prefix}{_encode_key(app_id)}:{shard_id}"


def _history_entries(value: Any) -> list[HistoryEntry]:
    if not isinstance(value, list):
        return []
    entries = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            entries.append(HistoryEntry(**item))
        except TypeError:
            continue
    return entries


def _ref_entry(value: Any) -> Optional[RefEntry]:
    if not isinstance(value, dict):
        return None
    payload = dict(value)
    attachments = payload.get("attachments")
    payload["attachments"] = tuple(
        attachment
        for attachment in (_attachment(item) for item in attachments or ())
        if attachment is not None
    )
    try:
        return RefEntry(**payload)
    except TypeError:
        return None


def _attachment_payload(attachment: InboundAttachment) -> dict[str, Any]:
    payload = asdict(attachment)
    payload["raw"] = dict(attachment.raw)
    return payload


def _attachment(value: Any) -> Optional[InboundAttachment]:
    if not isinstance(value, dict):
        return None
    try:
        return InboundAttachment(**value)
    except TypeError:
        return None


def _encode_key(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
