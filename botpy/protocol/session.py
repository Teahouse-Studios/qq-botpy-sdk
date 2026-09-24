import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import time
from typing import Callable, Dict, Optional, Protocol, Tuple, Union, runtime_checkable

from .models import SessionState


SessionKey = Tuple[str, int]


@runtime_checkable
class SessionStore(Protocol):
    """Gateway Resume 状态的异步持久化接口。"""

    async def load(self, app_id: str, shard_id: int) -> Optional[SessionState]: ...

    async def save(self, app_id: str, state: SessionState) -> None: ...

    async def clear(self, app_id: str, shard_id: int) -> None: ...

    async def close(self) -> None: ...


class MemorySessionStore:
    """带过期时间的进程内 SessionStore，适合测试和自定义适配器示例。"""

    def __init__(self, *, ttl: float = 300, clock: Callable[[], float] = time.time) -> None:
        self.ttl = max(0.0, ttl)
        self._clock = clock
        self._states: Dict[SessionKey, Tuple[SessionState, float]] = {}
        self._lock = asyncio.Lock()

    async def load(self, app_id: str, shard_id: int) -> Optional[SessionState]:
        key = (app_id, shard_id)
        async with self._lock:
            stored = self._states.get(key)
            if stored is None:
                return None
            state, saved_at = stored
            if self._clock() - saved_at > self.ttl:
                self._states.pop(key, None)
                return None
            return state

    async def save(self, app_id: str, state: SessionState) -> None:
        async with self._lock:
            self._states[(app_id, state.shard_id)] = (state, self._clock())

    async def clear(self, app_id: str, shard_id: int) -> None:
        async with self._lock:
            self._states.pop((app_id, shard_id), None)

    async def close(self) -> None:
        return None


class JsonFileSessionStore:
    """按机器人和分片保存独立 JSON 文件的 SessionStore。"""

    def __init__(
        self,
        directory: Union[str, os.PathLike[str]],
        *,
        ttl: float = 300,
        save_throttle: float = 1,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.directory = Path(directory)
        self.ttl = max(0.0, ttl)
        self.save_throttle = max(0.0, save_throttle)
        self._clock = clock
        self._logger = logger or logging.getLogger("botpy.protocol.session")
        self._lock = asyncio.Lock()
        self._last_saved: Dict[SessionKey, float] = {}
        self._pending: Dict[SessionKey, SessionState] = {}
        self._flush_tasks: Dict[SessionKey, asyncio.Task] = {}

    async def load(self, app_id: str, shard_id: int) -> Optional[SessionState]:
        self._validate_identity(app_id, shard_id)
        async with self._lock:
            return await asyncio.to_thread(self._load_sync, app_id, shard_id)

    async def save(self, app_id: str, state: SessionState) -> None:
        self._validate_identity(app_id, state.shard_id)
        if not state.session_id or state.sequence is None:
            return

        key = (app_id, state.shard_id)
        async with self._lock:
            last_saved = self._last_saved.get(key)
            elapsed = self._clock() - last_saved if last_saved is not None else self.save_throttle
            if elapsed >= self.save_throttle and key not in self._flush_tasks:
                await asyncio.to_thread(self._save_sync, app_id, state)
                self._last_saved[key] = self._clock()
                return

            self._pending[key] = state
            if key not in self._flush_tasks:
                delay = max(0.0, self.save_throttle - elapsed)
                self._flush_tasks[key] = asyncio.create_task(self._flush_after(key, delay))

    async def clear(self, app_id: str, shard_id: int) -> None:
        self._validate_identity(app_id, shard_id)
        key = (app_id, shard_id)
        task = None
        async with self._lock:
            task = self._flush_tasks.pop(key, None)
            if task and task is not asyncio.current_task():
                task.cancel()
            self._pending.pop(key, None)
            self._last_saved.pop(key, None)
            await asyncio.to_thread(self._remove_sync, app_id, shard_id)
        if task and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        while True:
            tasks = tuple(self._flush_tasks.values())
            if not tasks:
                break
            await asyncio.gather(*tasks, return_exceptions=True)

        async with self._lock:
            pending = tuple(self._pending.items())
            self._pending.clear()
            for (app_id, _shard_id), state in pending:
                await asyncio.to_thread(self._save_sync, app_id, state)

    async def _flush_after(self, key: SessionKey, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            async with self._lock:
                state = self._pending.pop(key, None)
                if state is not None:
                    await asyncio.to_thread(self._save_sync, key[0], state)
                    self._last_saved[key] = self._clock()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._logger.warning("[botpy] 保存 Gateway Session 失败: %s", exc)
        finally:
            self._flush_tasks.pop(key, None)

    def _load_sync(self, app_id: str, shard_id: int) -> Optional[SessionState]:
        path = self._path_for(app_id, shard_id)
        if path.is_symlink():
            self._remove_path(path)
            return None
        if not path.exists():
            return None
        try:
            self._restrict_file(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            saved_at = float(payload["saved_at"])
            if self._clock() - saved_at > self.ttl:
                self._remove_path(path)
                return None
            if payload.get("app_id") != app_id or payload.get("shard_id") != shard_id:
                self._remove_path(path)
                return None
            session_id = payload.get("session_id")
            sequence = payload.get("sequence")
            shard_count = payload.get("shard_count")
            if (
                not isinstance(session_id, str)
                or not session_id
                or not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or not isinstance(shard_count, int)
                or shard_count < 1
            ):
                self._remove_path(path)
                return None
            return SessionState(
                session_id=session_id,
                sequence=sequence,
                shard_id=shard_id,
                shard_count=shard_count,
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self._logger.warning("[botpy] 无法读取 Gateway Session %s: %s", path, exc)
            self._remove_path(path)
            return None

    def _save_sync(self, app_id: str, state: SessionState) -> None:
        path = self._path_for(app_id, state.shard_id)
        temporary_path = path.with_suffix(path.suffix + f".{os.getpid()}-{id(self)}.tmp")
        payload = {
            "app_id": app_id,
            "session_id": state.session_id,
            "sequence": state.sequence,
            "shard_id": state.shard_id,
            "shard_count": state.shard_count,
            "saved_at": self._clock(),
        }
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.directory, 0o700)
        except (OSError, NotImplementedError):
            pass
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary_path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            os.replace(temporary_path, path)
        except BaseException:
            self._remove_path(temporary_path)
            raise

    def _remove_sync(self, app_id: str, shard_id: int) -> None:
        self._remove_path(self._path_for(app_id, shard_id))

    @staticmethod
    def _remove_path(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except (OSError, NotImplementedError):
            pass

    def _path_for(self, app_id: str, shard_id: int) -> Path:
        self._validate_identity(app_id, shard_id)
        encoded_app_id = base64.urlsafe_b64encode(app_id.encode("utf-8")).decode("ascii").rstrip("=")
        return self.directory / f"session-{encoded_app_id}-{shard_id}.json"

    @staticmethod
    def _validate_identity(app_id: str, shard_id: int) -> None:
        if not isinstance(app_id, str) or not app_id:
            raise ValueError("app_id must be a non-empty string")
        if isinstance(shard_id, bool) or not isinstance(shard_id, int) or shard_id < 0:
            raise ValueError("shard_id must be a non-negative integer")

    @staticmethod
    def _restrict_file(path: Path) -> None:
        """Keep persisted Gateway resume credentials private on POSIX hosts."""

        try:
            os.chmod(path, 0o600, follow_symlinks=False)
        except OSError:
            # chmod is unavailable or restricted on some filesystems/Windows;
            # the atomic replace still prevents partial writes.
            pass


FileSessionStore = JsonFileSessionStore
