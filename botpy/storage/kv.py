import asyncio
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Dict, Optional, Protocol, Union, runtime_checkable


@runtime_checkable
class KVStore(Protocol):
    """支持 TTL 的异步键值存储接口。"""

    async def get(self, key: str) -> Any: ...

    async def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None: ...

    async def delete(self, key: str) -> bool: ...

    async def has(self, key: str) -> bool: ...

    async def keys(self, prefix: Optional[str] = None) -> list[str]: ...

    async def clear(self, prefix: Optional[str] = None) -> None: ...


@dataclass
class _Entry:
    value: Any
    expire_at: Optional[float] = None


class MemoryKVStore:
    """适合测试和单进程临时状态的内存 KV Store。"""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._entries: Dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if self._expired(entry):
                self._entries.pop(key, None)
                return None
            return entry.value

    async def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        expire_at = self._clock() + ttl if ttl is not None and ttl > 0 else None
        async with self._lock:
            self._entries[key] = _Entry(value=value, expire_at=expire_at)

    async def delete(self, key: str) -> bool:
        async with self._lock:
            return self._entries.pop(key, None) is not None

    async def has(self, key: str) -> bool:
        return await self.get(key) is not None

    async def keys(self, prefix: Optional[str] = None) -> list[str]:
        async with self._lock:
            self._purge_expired()
            keys = list(self._entries)
        return [key for key in keys if prefix is None or key.startswith(prefix)]

    async def clear(self, prefix: Optional[str] = None) -> None:
        async with self._lock:
            if prefix is None:
                self._entries.clear()
            else:
                for key in tuple(self._entries):
                    if key.startswith(prefix):
                        self._entries.pop(key, None)

    async def close(self) -> None:
        return None

    async def size(self) -> int:
        async with self._lock:
            self._purge_expired()
            return len(self._entries)

    def _expired(self, entry: _Entry) -> bool:
        return entry.expire_at is not None and entry.expire_at <= self._clock()

    def _purge_expired(self) -> None:
        for key, entry in tuple(self._entries.items()):
            if self._expired(entry):
                self._entries.pop(key, None)


class JsonFileKVStore:
    """单进程 JSON 文件 KV Store，支持节流保存和原子替换。"""

    def __init__(
        self,
        directory: Union[str, os.PathLike[str]],
        *,
        file_name: str = "kv-store.json",
        save_throttle: float = 1,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.path = Path(directory) / file_name
        self.save_throttle = max(0.0, save_throttle)
        self._clock = clock
        self._logger = logger or logging.getLogger("botpy.storage.kv")
        self._entries: Dict[str, _Entry] = {}
        self._loaded = False
        self._dirty = False
        self._flush_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any:
        async with self._lock:
            await self._ensure_loaded()
            entry = self._entries.get(key)
            if entry is None:
                return None
            if self._expired(entry):
                self._entries.pop(key, None)
                await self._schedule_save()
                return None
            return entry.value

    async def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        expire_at = self._clock() + ttl if ttl is not None and ttl > 0 else None
        async with self._lock:
            await self._ensure_loaded()
            self._entries[key] = _Entry(value=value, expire_at=expire_at)
            await self._schedule_save()

    async def delete(self, key: str) -> bool:
        async with self._lock:
            await self._ensure_loaded()
            removed = self._entries.pop(key, None) is not None
            if removed:
                await self._schedule_save()
            return removed

    async def has(self, key: str) -> bool:
        return await self.get(key) is not None

    async def keys(self, prefix: Optional[str] = None) -> list[str]:
        async with self._lock:
            await self._ensure_loaded()
            removed = self._purge_expired()
            if removed:
                await self._schedule_save()
            keys = list(self._entries)
        return [key for key in keys if prefix is None or key.startswith(prefix)]

    async def clear(self, prefix: Optional[str] = None) -> None:
        async with self._lock:
            await self._ensure_loaded()
            if prefix is None:
                changed = bool(self._entries)
                self._entries.clear()
            else:
                matched = [key for key in self._entries if key.startswith(prefix)]
                changed = bool(matched)
                for key in matched:
                    self._entries.pop(key, None)
            if changed:
                await self._schedule_save()

    async def flush(self) -> None:
        task = None
        async with self._lock:
            await self._ensure_loaded()
            task = self._flush_task
            self._flush_task = None
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            if self._dirty:
                await self._save()

    async def close(self) -> None:
        await self.flush()

    async def size(self) -> int:
        return len(await self.keys())

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        entries = await asyncio.to_thread(self._load_sync)
        self._entries = entries
        self._loaded = True

    async def _schedule_save(self) -> None:
        self._dirty = True
        if self.save_throttle == 0:
            await self._save()
            return
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_after_delay())

    async def _flush_after_delay(self) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(self.save_throttle)
            async with self._lock:
                if self._dirty:
                    await self._save()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._logger.warning("[botpy] 保存 KV Store 失败: %s", exc)
        finally:
            if self._flush_task is current:
                self._flush_task = None

    async def _save(self) -> None:
        snapshot = {
            key: {"value": entry.value, "expire_at": entry.expire_at}
            for key, entry in self._entries.items()
        }
        try:
            await asyncio.to_thread(self._save_sync, snapshot)
        except (OSError, TypeError, ValueError) as exc:
            self._logger.warning("[botpy] 保存 KV Store 失败: %s", exc)
            return
        self._dirty = False

    def _load_sync(self) -> Dict[str, _Entry]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            raw_entries = payload.get("entries", payload)
            if not isinstance(raw_entries, dict):
                return {}
            entries = {}
            for key, value in raw_entries.items():
                if not isinstance(key, str) or not isinstance(value, dict) or "value" not in value:
                    continue
                expire_at = value.get("expire_at")
                entry = _Entry(
                    value=value["value"],
                    expire_at=float(expire_at) if expire_at is not None else None,
                )
                if not self._expired(entry):
                    entries[key] = entry
            return entries
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._logger.warning("[botpy] 读取 KV Store %s 失败: %s", self.path, exc)
            return {}

    def _save_sync(self, entries: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + f".{os.getpid()}-{id(self)}.tmp")
        payload = {"version": 1, "entries": entries}
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def _expired(self, entry: _Entry) -> bool:
        return entry.expire_at is not None and entry.expire_at <= self._clock()

    def _purge_expired(self) -> bool:
        removed = False
        for key, entry in tuple(self._entries.items()):
            if self._expired(entry):
                self._entries.pop(key, None)
                removed = True
        return removed


FileKVStore = JsonFileKVStore
