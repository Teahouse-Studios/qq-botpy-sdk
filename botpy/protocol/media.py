import asyncio
import hashlib
import inspect
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Union

from .. import logging
from .errors import UploadDailyLimitExceededError
from .message import CHUNKED_MEDIA_MAX_SIZE, MEDIA_FILE_SIZE_LIMITS, MediaFileType
from .upload_cache import UploadCache


MD5_10M_SIZE = 10_002_432
DEFAULT_CONCURRENT_PARTS = 1
MAX_CONCURRENT_PARTS = 10
DEFAULT_PART_FINISH_RETRY_TIMEOUT = 120.0
MAX_PART_FINISH_RETRY_TIMEOUT = 600.0
PART_FINISH_RETRY_INTERVAL = 1.0
UPLOAD_PREPARE_DAILY_LIMIT_CODE = 40093002
PART_FINISH_RETRYABLE_CODE = 40093001


class ChunkedUploadApi(Protocol):
    async def post_upload_prepare(self, scope: str, target_id: str, **payload: Any) -> Mapping[str, Any]: ...

    async def put_upload_part(self, presigned_url: str, data: bytes, *, timeout: float = 300) -> Any: ...

    async def post_upload_part_finish(self, scope: str, target_id: str, **payload: Any) -> Any: ...

    async def post_upload_complete(self, scope: str, target_id: str, **payload: Any) -> Mapping[str, Any]: ...


ProgressCallback = Callable[[int, int], Union[Awaitable[None], None]]


@dataclass(frozen=True)
class ChunkedUploadProgress:
    completed_parts: int
    total_parts: int
    uploaded_bytes: int
    total_bytes: int


@dataclass(frozen=True)
class UploadHashes:
    md5: str
    sha1: str
    md5_10m: str


@dataclass(frozen=True)
class _UploadPart:
    index: int
    presigned_url: str


class ChunkedMediaUploader:
    """执行 QQ 开放平台的大文件分片上传流程。"""

    def __init__(
        self,
        api: ChunkedUploadApi,
        *,
        logger: Any = None,
        upload_cache: Optional[UploadCache] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self.logger = logger or logging.get_logger()
        self.upload_cache = upload_cache
        self._sleep = sleep
        self._clock = clock

    async def upload(
        self,
        scope: str,
        target_id: str,
        file_type: Union[int, MediaFileType],
        *,
        data: Optional[bytes] = None,
        local_path: Optional[Union[str, Path]] = None,
        file_name: Optional[str] = None,
        on_progress: Optional[ProgressCallback] = None,
    ) -> Mapping[str, Any]:
        if scope not in ("c2c", "group"):
            raise ValueError("chunked upload is only supported for c2c and group targets")
        if not isinstance(target_id, str) or not target_id:
            raise ValueError("target_id is required")
        if (data is None) == (local_path is None):
            raise ValueError("exactly one of data or local_path is required for chunked upload")
        if file_name is not None and not isinstance(file_name, str):
            raise TypeError("file_name must be a string")
        if on_progress is not None and not callable(on_progress):
            raise TypeError("on_progress must be callable")
        try:
            normalized_type = MediaFileType(file_type)
        except (TypeError, ValueError) as exc:
            raise ValueError("file_type must be a valid MediaFileType") from exc
        if isinstance(file_type, bool):
            raise ValueError("file_type must be a valid MediaFileType")

        path: Optional[Path] = None
        if data is not None:
            if not isinstance(data, bytes):
                raise TypeError("data must be bytes")
            file_size = len(data)
            display_name = file_name or "file"
            path_label = "<bytes>"
        else:
            path = Path(local_path)
            if path.is_symlink() or not path.is_file():
                raise ValueError("local_path must point to a regular file and may not be a symlink")
            file_size = path.stat().st_size
            display_name = file_name or path.name
            path_label = str(path)

        self._validate_size(normalized_type, file_size)
        hashes = (
            await asyncio.to_thread(_hash_bytes, data)
            if data is not None
            else await asyncio.to_thread(_hash_file, path)
        )
        if self.upload_cache is not None:
            cached = self.upload_cache.get(hashes.md5, scope, target_id, normalized_type)
            if cached is not None:
                return {"file_uuid": "", "file_info": cached, "ttl": 0, "cached": True}
        prepare_name = _sanitize_file_name(display_name) if normalized_type == MediaFileType.FILE else display_name
        try:
            prepared = await self.api.post_upload_prepare(
                scope,
                target_id,
                file_type=int(normalized_type),
                file_name=prepare_name,
                file_size=file_size,
                md5=hashes.md5,
                sha1=hashes.sha1,
                md5_10m=hashes.md5_10m,
            )
        except Exception as exc:
            if _error_code(exc) == UPLOAD_PREPARE_DAILY_LIMIT_CODE:
                raise UploadDailyLimitExceededError(path_label, file_size, str(exc)) from exc
            raise

        upload_id, block_size, parts, concurrency, retry_timeout = _parse_prepare_response(prepared, file_size)
        semaphore = asyncio.Semaphore(concurrency)
        completed_parts = 0
        uploaded_bytes = 0

        async def upload_part(part: _UploadPart) -> None:
            nonlocal completed_parts, uploaded_bytes
            async with semaphore:
                offset = (part.index - 1) * block_size
                length = min(block_size, file_size - offset)
                if length <= 0:
                    raise ValueError(f"upload part {part.index} points outside the source file")
                if data is not None:
                    part_data = data[offset : offset + length]
                else:
                    part_data = await asyncio.to_thread(_read_file_part, path, offset, length)
                if len(part_data) != length:
                    raise OSError(f"upload source changed while reading part {part.index}")

                part_md5 = hashlib.md5(part_data).hexdigest()
                await self.api.put_upload_part(part.presigned_url, part_data, timeout=300)
                await self._finish_part(
                    scope,
                    target_id,
                    upload_id=upload_id,
                    part_index=part.index,
                    block_size=length,
                    md5=part_md5,
                    retry_timeout=retry_timeout,
                )
                completed_parts += 1
                uploaded_bytes += length
                if on_progress is not None:
                    callback_result = on_progress(uploaded_bytes, file_size)
                    if inspect.isawaitable(callback_result):
                        await callback_result

        for start in range(0, len(parts), concurrency):
            tasks = [asyncio.create_task(upload_part(part)) for part in parts[start : start + concurrency]]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        completed = await self.api.post_upload_complete(scope, target_id, upload_id=upload_id)
        if self.upload_cache is not None and isinstance(completed, Mapping):
            self.upload_cache.store_response(hashes.md5, scope, target_id, normalized_type, completed)
        return completed

    async def _finish_part(
        self,
        scope: str,
        target_id: str,
        *,
        upload_id: str,
        part_index: int,
        block_size: int,
        md5: str,
        retry_timeout: float,
    ) -> None:
        deadline = self._clock() + retry_timeout
        while True:
            try:
                await self.api.post_upload_part_finish(
                    scope,
                    target_id,
                    upload_id=upload_id,
                    part_index=part_index,
                    block_size=block_size,
                    md5=md5,
                )
                return
            except Exception as exc:
                if _error_code(exc) != PART_FINISH_RETRYABLE_CODE:
                    raise
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise
                delay = min(PART_FINISH_RETRY_INTERVAL, remaining)
                self.logger.warning(
                    "[botpy] 分片 %d 确认暂未就绪，%.1f 秒后重试",
                    part_index,
                    delay,
                )
                await self._sleep(delay)

    @staticmethod
    def _validate_size(file_type: MediaFileType, file_size: int) -> None:
        if file_size <= 0:
            raise ValueError("media file must not be empty")
        limit = MEDIA_FILE_SIZE_LIMITS.get(int(file_type), CHUNKED_MEDIA_MAX_SIZE)
        if file_size > limit:
            raise ValueError(
                f"media is too large for {file_type.name.lower()}; limit is {limit // (1024 * 1024)} MiB"
            )


def _parse_prepare_response(
    response: Mapping[str, Any],
    file_size: int,
) -> tuple[str, int, list[_UploadPart], int, float]:
    if not isinstance(response, Mapping):
        raise ValueError("upload_prepare returned a non-object response")
    upload_id = response.get("upload_id")
    block_size = response.get("block_size")
    raw_parts = response.get("parts")
    if not isinstance(upload_id, str) or not upload_id:
        raise ValueError("upload_prepare response is missing upload_id")
    if not isinstance(block_size, int) or isinstance(block_size, bool) or block_size <= 0:
        raise ValueError("upload_prepare response contains an invalid block_size")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ValueError("upload_prepare response is missing parts")

    parts = []
    seen_indexes = set()
    for raw_part in raw_parts:
        if not isinstance(raw_part, Mapping):
            raise ValueError("upload_prepare response contains an invalid part")
        index = raw_part.get("index")
        url = raw_part.get("presigned_url")
        if not isinstance(index, int) or isinstance(index, bool) or index < 1 or index in seen_indexes:
            raise ValueError("upload_prepare response contains an invalid part index")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise ValueError("upload_prepare response contains an invalid presigned_url")
        if (index - 1) * block_size >= file_size:
            raise ValueError("upload_prepare response contains a part outside the source file")
        seen_indexes.add(index)
        parts.append(_UploadPart(index=index, presigned_url=url))

    expected_indexes = set(range(1, (file_size + block_size - 1) // block_size + 1))
    if seen_indexes != expected_indexes:
        raise ValueError("upload_prepare response parts do not cover the complete source file")
    parts.sort(key=lambda part: part.index)

    concurrency = response.get("concurrency", DEFAULT_CONCURRENT_PARTS)
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency <= 0:
        concurrency = DEFAULT_CONCURRENT_PARTS
    concurrency = min(concurrency, MAX_CONCURRENT_PARTS)

    raw_retry_timeout = response.get("retry_timeout")
    if (
        isinstance(raw_retry_timeout, (int, float))
        and not isinstance(raw_retry_timeout, bool)
        and raw_retry_timeout > 0
    ):
        retry_timeout = min(max(float(raw_retry_timeout), 0.0), MAX_PART_FINISH_RETRY_TIMEOUT)
    else:
        retry_timeout = DEFAULT_PART_FINISH_RETRY_TIMEOUT
    return upload_id, block_size, parts, concurrency, retry_timeout


def _hash_bytes(data: bytes) -> UploadHashes:
    md5 = hashlib.md5(data).hexdigest()
    sha1 = hashlib.sha1(data).hexdigest()
    md5_10m = hashlib.md5(data[:MD5_10M_SIZE]).hexdigest() if len(data) > MD5_10M_SIZE else md5
    return UploadHashes(md5=md5, sha1=sha1, md5_10m=md5_10m)


def _hash_file(path: Path) -> UploadHashes:
    md5_hash = hashlib.md5()
    sha1_hash = hashlib.sha1()
    md5_10m_hash = hashlib.md5()
    consumed = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            md5_hash.update(chunk)
            sha1_hash.update(chunk)
            remaining = MD5_10M_SIZE - consumed
            if remaining > 0:
                md5_10m_hash.update(chunk[:remaining])
            consumed += len(chunk)
    md5 = md5_hash.hexdigest()
    md5_10m = md5_10m_hash.hexdigest() if consumed > MD5_10M_SIZE else md5
    return UploadHashes(md5=md5, sha1=sha1_hash.hexdigest(), md5_10m=md5_10m)


def _read_file_part(path: Path, offset: int, length: int) -> bytes:
    with path.open("rb") as source:
        source.seek(offset)
        return source.read(length)


def _sanitize_file_name(file_name: str) -> str:
    if not isinstance(file_name, str):
        raise TypeError("file_name must be a string")
    invalid = r'\/:*?"<>|'
    cleaned = "".join("_" if char in invalid or ord(char) < 32 or ord(char) == 127 else char for char in file_name)
    return " ".join(cleaned.split()).strip() or "file"


def _error_code(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.lstrip("-").isdigit():
        return int(code)
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        raw_code = response.get("err_code", response.get("code"))
        if isinstance(raw_code, int):
            return raw_code
        if isinstance(raw_code, str) and raw_code.lstrip("-").isdigit():
            return int(raw_code)
    return None
