# -*- coding: utf-8 -*-
"""素材管理（media image）上传流程。

平台要求先把 Markdown 消息要用的图片上传换取 ``qqbot-media://`` 引用：

1. ``POST /v2/media-images/prepare`` 申请上传，得到预签名 URL；
2. 用 HTTP ``PUT`` 把图片二进制直传到预签名 URL；
3. ``POST /v2/media-images/complete`` 完成上传，得到 ``image_id`` 与引用链接。

单机器人默认最多保留 100 张有效图片，超限时 prepare/complete 返回
``40035007``。本模块除了串起三步，还维护一份本地列表镜像
（:class:`MediaImageRegistry`）：启动时同步一次，之后上传插队首、删除弹队尾，
不必每次释放配额都重新翻分页；每次上传成功后还会派发后台任务检查容量水位
（:class:`MediaImageCapacityPolicy`），达到 80% 时删除末尾（最旧）的一张图片。
"""

import asyncio
import hashlib
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Collection
from typing import Any, Awaitable, Callable, Deque, List, Mapping, Optional, Protocol, Sequence, Set, Tuple, Union

from .. import logging
from ..types.media_image import (
    MEDIA_IMAGE_EXTENSIONS,
    MEDIA_IMAGE_MAX_COUNT,
    MEDIA_IMAGE_MAX_SIZE,
    MEDIA_IMAGE_NOT_FOUND_CODE,
    MEDIA_IMAGE_QUOTA_EXCEEDED_CODE,
)
from .errors import MediaImageQuotaExceededError, extract_exception_error_code
from .image import ImageSize, parse_image_size


class MediaImageUploadApi(Protocol):
    """``MediaImageUploader`` 需要的最小 API 集合，便于测试替换。"""

    async def post_media_image_prepare(self, **payload: Any) -> Mapping[str, Any]: ...

    async def put_media_image(self, presigned_url: str, data: bytes, **kwargs: Any) -> Any: ...

    async def post_media_image_complete(self, **payload: Any) -> Mapping[str, Any]: ...

    async def get_media_images(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def delete_media_image(self, image_id: str) -> Any: ...


@dataclass(frozen=True)
class MediaImageQuotaPolicy:
    """素材图片数量超限时的自动释放策略。

    ``delete_per_round`` 为 ``None`` 时整页删除（平台列表按上传时间倒序，最后一页是
    最旧的一批图片）；给正整数则每轮只删这么多张。``protect`` 里的 ``image_id``
    永不会被删除，适合把正在使用的图片排除在外。
    """

    enabled: bool = True
    max_rounds: int = 3
    delete_per_round: Optional[int] = None
    protect: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("quota policy enabled must be a bool")
        if isinstance(self.max_rounds, bool) or not isinstance(self.max_rounds, int) or self.max_rounds < 1:
            raise ValueError("quota policy max_rounds must be a positive integer")
        if self.delete_per_round is not None:
            if (
                isinstance(self.delete_per_round, bool)
                or not isinstance(self.delete_per_round, int)
                or self.delete_per_round < 1
            ):
                raise ValueError("quota policy delete_per_round must be a positive integer or None")
        protect = self.protect
        if isinstance(protect, str) or not isinstance(protect, Sequence):
            raise TypeError("quota policy protect must be a sequence of image ids")
        if any(not isinstance(item, str) or not item for item in protect):
            raise ValueError("quota policy protect must contain non-empty strings")
        object.__setattr__(self, "protect", tuple(protect))


@dataclass(frozen=True)
class MediaImageCapacityPolicy:
    """上传后的容量水位检查策略。

    每次上传成功都会派发一个后台任务检查本地镜像里的有效图片数量；达到
    ``high_watermark``（默认 80%）时删除列表末尾（最旧）的一张图片。``protect``
    中的 ``image_id`` 永远不会被删除，刚上传的图片也会自动排除在外。
    """

    enabled: bool = True
    high_watermark: float = 0.8
    max_count: int = MEDIA_IMAGE_MAX_COUNT
    protect: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("capacity policy enabled must be a bool")
        if isinstance(self.high_watermark, bool) or not isinstance(self.high_watermark, (int, float)):
            raise TypeError("capacity policy high_watermark must be a number")
        if not 0 < float(self.high_watermark) <= 1:
            raise ValueError("capacity policy high_watermark must be in (0, 1]")
        if isinstance(self.max_count, bool) or not isinstance(self.max_count, int) or self.max_count < 1:
            raise ValueError("capacity policy max_count must be a positive integer")
        protect = self.protect
        if isinstance(protect, str) or not isinstance(protect, Sequence):
            raise TypeError("capacity policy protect must be a sequence of image ids")
        if any(not isinstance(item, str) or not item for item in protect):
            raise ValueError("capacity policy protect must contain non-empty strings")
        object.__setattr__(self, "protect", tuple(protect))

    @property
    def threshold(self) -> int:
        """触发水位检查的图片数量。"""

        return max(1, math.ceil(float(self.high_watermark) * self.max_count))


@dataclass(frozen=True)
class MediaImageQuotaRelease:
    """一次配额释放的结果。"""

    total: Optional[int]
    deleted: Tuple[str, ...]

    @property
    def deleted_count(self) -> int:
        return len(self.deleted)


@dataclass(frozen=True)
class MediaImageUploadResult:
    """一次素材图片上传的结果。"""

    image_id: str
    url: str
    ttl: int
    preview_url: Optional[str]
    upload_id: str
    file_name: str
    content_type: str
    size: int
    markdown: str
    released_images: Tuple[str, ...] = ()
    """上传前为了腾出配额而自动删除的 ``image_id``。"""


def guess_media_image_content_type(file_name: str) -> Optional[str]:
    """按扩展名返回平台的 MIME 类型；不支持的扩展名返回 ``None``。"""

    if not isinstance(file_name, str) or not file_name:
        return None
    return MEDIA_IMAGE_EXTENSIONS.get(Path(file_name).suffix.lower())


def format_media_image_markdown(url: str, *, alt: str = "", size: Optional[ImageSize] = None) -> str:
    """生成 Markdown 图片语法；已知宽高时附带 ``#宽px #高px``。"""

    if not isinstance(url, str) or not url:
        raise ValueError("url must be a non-empty string")
    if size is None:
        return f"![{alt}]({url})"
    return f"![{alt} #{size.width}px #{size.height}px]({url})"


def _hash_media_image(data: bytes) -> Tuple[str, str]:
    return hashlib.md5(data).hexdigest(), hashlib.sha1(data).hexdigest()


class MediaImageRegistry:
    """本地维护的素材图片列表镜像。

    平台 ``GET /v2/media-images`` 按上传时间倒序返回：首页最新、末页最旧。镜像
    保持同样的顺序（``entries[0]`` 最新、``entries[-1]`` 最旧），上传成功后插到
    队首，释放配额时从队尾弹出，删除图片不再需要每次重新翻完全部分页。

    并发的后台检查任务执行顺序不受控，因此所有删除都在同一把
    :attr:`_delete_lock` 内完成「弹出一张 → 调用删除接口」，保证末尾弹出严格按
    顺序进行，不会有两个任务取到同一张图片。
    """

    def __init__(self, api: MediaImageUploadApi, *, logger: Any = None) -> None:
        self.api = api
        self.logger = logger or logging.get_logger()
        self._entries: List[str] = []
        self._total: Optional[int] = None
        self._last_page_size = 1
        self._loaded = False
        self._sync_lock = asyncio.Lock()
        self._delete_lock = asyncio.Lock()
        self._tasks: Set[asyncio.Task] = set()
        self._recent: Deque[str] = deque(maxlen=1024)
        # 已确认删除的 image_id。远端列表是最终一致的，同步时若仍看到这些 id，
        # 说明快照落后于本次删除，必须在合并阶段过滤掉，否则会把已删图片「复活」。
        self._deleted_ids: Deque[str] = deque(maxlen=4096)
        self._closed = False

    @property
    def loaded(self) -> bool:
        """是否已经和远端同步过。"""

        return self._loaded

    @property
    def total(self) -> Optional[int]:
        """本地镜像记录的有效图片总数；未同步时为 ``None``。"""

        return self._total

    @property
    def page_size(self) -> int:
        """最近一次同步时最后一页的条数，用于「整页释放」。"""

        return self._last_page_size

    @property
    def entries(self) -> Tuple[str, ...]:
        """当前镜像的 ``image_id``，按上传时间倒序（最新在前）。"""

        return tuple(self._entries)

    async def sync(self) -> Optional[int]:
        """全量同步远端列表，返回同步后的有效图片总数。

        翻页请求不持锁（避免长时间阻塞删除），但把快照写回镜像时与删除共用同一把
        锁，并按 ``_deleted_ids`` 过滤掉快照里尚未消失的已删图片。
        """

        async with self._sync_lock:
            remote_ids, remote_total, last_page_size = await self._fetch_remote()
            async with self._delete_lock:
                self._apply_snapshot(remote_ids, remote_total, last_page_size)
                return self._total

    async def _fetch_remote(self) -> Tuple[List[str], Optional[int], int]:
        """翻完全部分页，返回 ``(image_id 列表, total, 最后一页条数)``。"""

        cursor: Optional[str] = None
        seen: Set[str] = set()
        ids: List[str] = []
        total: Optional[int] = None
        last_page_size = 0
        while True:
            page = await self.api.get_media_images(cursor=cursor)
            if not isinstance(page, Mapping):
                break
            page_ids = _page_image_ids(page)
            ids.extend(page_ids)
            last_page_size = len(page_ids)
            value = page.get("total")
            if isinstance(value, int) and not isinstance(value, bool):
                total = value
            next_cursor = page.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                break
            seen.add(next_cursor)
            cursor = next_cursor
        return list(dict.fromkeys(ids)), total, max(last_page_size, 1)

    def _apply_snapshot(self, remote_ids: List[str], remote_total: Optional[int], last_page_size: int) -> None:
        """把一次远端快照合并进本地镜像（调用方须持有 ``_delete_lock``）。"""

        deleted = set(self._deleted_ids)
        stale = [image_id for image_id in remote_ids if image_id in deleted]
        if stale:
            remote_ids = [image_id for image_id in remote_ids if image_id not in deleted]
        # ``_recent`` 按上传时间先后排列：逐张插到队首，最后插入的（最新的）在最前。
        # 只补偿「远端快照里还没有」的上传；已经在列表里出现过的记录随之出队，
        # 否则它们日后被别处删掉时会被错误地重新插回镜像。
        pending = [image_id for image_id in self._recent if image_id not in remote_ids and image_id not in deleted]
        merged = list(dict.fromkeys(list(reversed(pending)) + remote_ids))
        self._entries = merged
        if remote_total is not None:
            self._total = max(0, remote_total - len(stale)) + len(pending)
        else:
            self._total = len(merged)
        self._recent = deque(
            (image_id for image_id in self._recent if image_id not in remote_ids),
            maxlen=self._recent.maxlen,
        )
        self._last_page_size = max(last_page_size, 1)
        self._loaded = True

    async def ensure_loaded(self) -> Optional[int]:
        """首次使用时懒同步一次；已同步过则直接返回本地总数。"""

        if self._loaded:
            return self._total
        return await self.sync()

    def record_upload(self, image_id: str) -> None:
        """把刚上传成功的图片记到镜像队首（最新）。"""

        if not isinstance(image_id, str) or not image_id:
            return
        self._recent.append(image_id)
        if image_id in self._entries:
            return
        self._entries.insert(0, image_id)
        if self._loaded:
            self._total = (self._total or 0) + 1

    def forget(self, image_id: str) -> None:
        """从镜像里移除一张已删除的图片。"""

        if image_id in self._entries:
            self._entries.remove(image_id)
            if self._loaded and self._total is not None:
                self._total = max(0, self._total - 1)
        if image_id in self._recent:
            self._recent.remove(image_id)

    async def delete(self, image_id: str) -> Any:
        """删除一张图片，并同步更新本地镜像。

        与容量水位触发的删除共用同一把锁，避免「手动删某张」和「后台弹队尾」交错
        执行时同一张图片被删两次。
        """

        async with self._delete_lock:
            # 按 id 删除不依赖完整列表：镜像未同步过时只删远端，本地不臆造条目。
            index = self._entries.index(image_id) if image_id in self._entries else None
            if index is not None:
                del self._entries[index]
                self._decrement_locked()
            try:
                result = await self.api.delete_media_image(image_id)
            except Exception as exc:
                if extract_exception_error_code(exc) == MEDIA_IMAGE_NOT_FOUND_CODE:
                    # 远端已经不存在，本地镜像保持移除状态即可。
                    self._remember_deleted(image_id)
                    return None
                if index is not None:
                    self._entries.insert(index, image_id)
                    self._increment_locked()
                raise
            self._remember_deleted(image_id)
            return result

    async def release_oldest(self, *, count: int = 1, protect: Collection[str] = ()) -> Tuple[str, ...]:
        """从队尾（最旧）依次删除 ``count`` 张图片，返回实际删除的 ``image_id``。"""

        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        if not self._loaded:
            await self.ensure_loaded()
        deleted: List[str] = []
        async with self._delete_lock:
            for _ in range(count):
                image_id = self._pop_oldest_locked(protect)
                if image_id is None:
                    break
                if await self._delete_locked(image_id):
                    deleted.append(image_id)
        return tuple(deleted)

    async def check_capacity(
        self,
        policy: MediaImageCapacityPolicy,
        *,
        protect: Collection[str] = (),
    ) -> Optional[str]:
        """检查容量水位，达到时删除队尾一张图片并返回其 ``image_id``。"""

        if not policy.enabled or self._closed:
            return None
        if not self._loaded:
            await self.ensure_loaded()
        async with self._delete_lock:
            current = self._total if self._total is not None else len(self._entries)
            if current < policy.threshold:
                return None
            protected = tuple(policy.protect) + tuple(protect)
            while True:
                image_id = self._pop_oldest_locked(protected)
                if image_id is None:
                    return None
                if await self._delete_locked(image_id):
                    break
            self.logger.info(
                "[botpy] 素材图片达到容量水位 %s/%s，已删除最旧的一张 %s",
                current,
                policy.max_count,
                image_id,
            )
            return image_id

    def schedule_capacity_check(
        self,
        policy: MediaImageCapacityPolicy,
        *,
        protect: Collection[str] = (),
    ) -> Optional["asyncio.Task[None]"]:
        """派发一个后台任务检查容量；任务异常只记日志，不影响上传结果。"""

        if not policy.enabled or self._closed:
            return None
        task = asyncio.create_task(self._run_capacity_check(policy, protect=tuple(protect)))
        self._track(task)
        return task

    def schedule_sync(self) -> Optional["asyncio.Task[None]"]:
        """派发一个后台任务，全量同步一次远端列表。"""

        if self._closed:
            return None
        task = asyncio.create_task(self._run_sync())
        self._track(task)
        return task

    async def aclose(self) -> None:
        """取消仍在进行的后台任务。"""

        self._closed = True
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    async def _run_capacity_check(
        self,
        policy: MediaImageCapacityPolicy,
        *,
        protect: Tuple[str, ...],
    ) -> None:
        try:
            await self.check_capacity(policy, protect=protect)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 后台任务不把异常抛回事件循环
            self.logger.warning("[botpy] 素材图片容量检查失败: %s", exc)

    async def _run_sync(self) -> None:
        try:
            await self.sync()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning("[botpy] 素材图片列表同步失败: %s", exc)

    def _track(self, task: "asyncio.Task[None]") -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _pop_oldest_locked(self, protect: Collection[str]) -> Optional[str]:
        protected = set(protect)
        for index in range(len(self._entries) - 1, -1, -1):
            image_id = self._entries[index]
            if image_id in protected:
                continue
            del self._entries[index]
            return image_id
        return None

    async def _delete_locked(self, image_id: str) -> bool:
        try:
            await self.api.delete_media_image(image_id)
        except Exception as exc:
            if extract_exception_error_code(exc) == MEDIA_IMAGE_NOT_FOUND_CODE:
                self._remember_deleted(image_id)
                self._decrement_locked()
                return False
            self._entries.append(image_id)
            raise
        self._remember_deleted(image_id)
        self._decrement_locked()
        return True

    def _remember_deleted(self, image_id: str) -> None:
        if image_id not in self._deleted_ids:
            self._deleted_ids.append(image_id)

    def _decrement_locked(self) -> None:
        if self._total is not None:
            self._total = max(0, self._total - 1)

    def _increment_locked(self) -> None:
        if self._total is not None:
            self._total += 1


class MediaImageUploader:
    """执行素材图片的 prepare → PUT → complete 三步上传。

    图片数量触顶时（``40035007``），会按 :class:`MediaImageQuotaPolicy` 从本地镜像
    的队尾（最旧）开始删除图片再重新申请上传；删除记录通过
    :attr:`MediaImageUploadResult.released_images` 返回。每次上传成功后还会按
    :class:`MediaImageCapacityPolicy` 派发后台任务检查容量水位。
    """

    def __init__(
        self,
        api: MediaImageUploadApi,
        *,
        logger: Any = None,
        quota_policy: Optional[MediaImageQuotaPolicy] = None,
        capacity_policy: Optional[MediaImageCapacityPolicy] = None,
        registry: Optional[MediaImageRegistry] = None,
    ) -> None:
        self.api = api
        self.logger = logger or logging.get_logger()
        self.quota_policy = quota_policy or MediaImageQuotaPolicy()
        self.capacity_policy = capacity_policy or MediaImageCapacityPolicy()
        self.registry = registry or MediaImageRegistry(api, logger=self.logger)

    async def upload(
        self,
        *,
        data: Optional[bytes] = None,
        local_path: Optional[Union[str, Path]] = None,
        file_name: Optional[str] = None,
        content_type: Optional[str] = None,
        alt: Optional[str] = None,
        timeout: float = 300,
        quota_policy: Optional[MediaImageQuotaPolicy] = None,
        capacity_policy: Optional[MediaImageCapacityPolicy] = None,
    ) -> MediaImageUploadResult:
        """上传一张图片并返回引用链接。

        ``data`` 与 ``local_path`` 必须二选一；``content_type`` 省略时按扩展名推断。
        ``alt`` 是 Markdown 图片的替代文本，省略时用文件名（不含扩展名）。
        ``quota_policy`` 省略时使用实例默认策略；``capacity_policy`` 同理。
        """

        if (data is None) == (local_path is None):
            raise ValueError("exactly one of data or local_path is required")
        if file_name is not None and not isinstance(file_name, str):
            raise TypeError("file_name must be a string")
        if content_type is not None and not isinstance(content_type, str):
            raise TypeError("content_type must be a string")
        if alt is not None and not isinstance(alt, str):
            raise TypeError("alt must be a string")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number")
        if quota_policy is not None and not isinstance(quota_policy, MediaImageQuotaPolicy):
            raise TypeError("quota_policy must be a MediaImageQuotaPolicy")
        if capacity_policy is not None and not isinstance(capacity_policy, MediaImageCapacityPolicy):
            raise TypeError("capacity_policy must be a MediaImageCapacityPolicy")
        policy = quota_policy or self.quota_policy
        capacity = capacity_policy or self.capacity_policy

        if data is not None:
            if not isinstance(data, bytes):
                raise TypeError("data must be bytes")
            payload = data
            display_name = file_name or "image.png"
        else:
            path = Path(local_path)
            if path.is_symlink() or not path.is_file():
                raise ValueError("local_path must point to a regular file and may not be a symlink")
            payload = await asyncio.to_thread(path.read_bytes)
            display_name = file_name or path.name

        if not payload:
            raise ValueError("image data must not be empty")
        file_size = len(payload)
        if file_size > MEDIA_IMAGE_MAX_SIZE:
            raise ValueError(f"image size {file_size} exceeds the {MEDIA_IMAGE_MAX_SIZE} byte limit")

        expected_type = guess_media_image_content_type(display_name)
        if expected_type is None:
            raise ValueError("file_name must end with .png, .jpg, .jpeg, .gif or .webp")
        if content_type is not None and content_type != expected_type:
            raise ValueError("content_type does not match the file_name extension")
        content_type = content_type or expected_type

        md5, sha1 = await asyncio.to_thread(_hash_media_image, payload)
        released: List[str] = []
        rounds = 0

        prepared, released, rounds = await self._call_with_quota_release(
            lambda: self.api.post_media_image_prepare(
                file_name=display_name,
                file_size=file_size,
                content_type=content_type,
                md5=md5,
                sha1=sha1,
            ),
            policy=policy,
            released=released,
            rounds=rounds,
        )
        upload_id = prepared.get("upload_id")
        presigned_url = prepared.get("presigned_url")
        if not upload_id or not presigned_url:
            raise RuntimeError("prepare 响应缺少 upload_id 或 presigned_url")

        await self.api.put_media_image(
            presigned_url,
            payload,
            content_type=content_type,
            timeout=timeout,
        )

        completed, released, _ = await self._call_with_quota_release(
            lambda: self.api.post_media_image_complete(upload_id=upload_id, md5=md5),
            policy=policy,
            released=released,
            rounds=rounds,
        )
        image_url = completed.get("url")
        if not image_url:
            raise RuntimeError("complete 响应缺少 url")

        size = parse_image_size(payload)
        markdown = format_media_image_markdown(
            image_url,
            alt=alt if alt is not None else Path(display_name).stem,
            size=size,
        )
        result = MediaImageUploadResult(
            image_id=str(completed.get("image_id") or ""),
            url=image_url,
            ttl=int(completed.get("ttl") or 0),
            preview_url=completed.get("preview_url"),
            upload_id=str(upload_id),
            file_name=display_name,
            content_type=content_type,
            size=file_size,
            markdown=markdown,
            released_images=tuple(released),
        )
        if result.image_id:
            self.registry.record_upload(result.image_id)
            self.registry.schedule_capacity_check(capacity, protect=(result.image_id,))
        return result

    async def _call_with_quota_release(
        self,
        call: Callable[[], Awaitable[Mapping[str, Any]]],
        *,
        policy: MediaImageQuotaPolicy,
        released: List[str],
        rounds: int,
    ) -> Tuple[Mapping[str, Any], List[str], int]:
        """执行一次平台调用；命中 40035007 时释放配额后重试。"""

        while True:
            try:
                return await call(), released, rounds
            except Exception as exc:
                if extract_exception_error_code(exc) != MEDIA_IMAGE_QUOTA_EXCEEDED_CODE:
                    raise
                if not policy.enabled:
                    raise MediaImageQuotaExceededError(
                        "media image quota is exhausted and automatic release is disabled",
                        deleted=len(released),
                        max_rounds=policy.max_rounds,
                    ) from exc
                if rounds >= policy.max_rounds:
                    raise MediaImageQuotaExceededError(
                        "media image quota is still exhausted after releasing the oldest page",
                        deleted=len(released),
                        max_rounds=policy.max_rounds,
                    ) from exc
                rounds += 1
                release = await self.release_quota(policy)
                if not release.deleted:
                    raise MediaImageQuotaExceededError(
                        "media image quota is exhausted and the oldest page has nothing to release",
                        total=release.total,
                        deleted=len(released),
                        max_rounds=policy.max_rounds,
                    ) from exc
                released.extend(release.deleted)

    async def release_quota(self, policy: Optional[MediaImageQuotaPolicy] = None) -> MediaImageQuotaRelease:
        """从本地镜像队尾（最旧）删除图片，腾出配额。

        ``protect`` 中的图片会被跳过；``delete_per_round`` 为 ``None`` 时按最近一次
        同步的页大小整页删除。删除时若平台返回「图片不存在」（并发删除），按已删除
        处理，不中断流程。
        """

        policy = policy or self.quota_policy
        if not isinstance(policy, MediaImageQuotaPolicy):
            raise TypeError("policy must be a MediaImageQuotaPolicy")

        # 先取「删除前」的总数，删完再报给调用方；镜像未同步过时同步一次。
        total = await self.registry.ensure_loaded()
        if total is None:
            total = self.registry.total
        count = policy.delete_per_round if policy.delete_per_round is not None else self.registry.page_size
        deleted = await self.registry.release_oldest(count=count, protect=policy.protect)
        if deleted:
            self.logger.info("[botpy] 素材图片配额已满，自动删除最旧的 %s 张", len(deleted))
        return MediaImageQuotaRelease(total=total, deleted=deleted)


def _page_image_ids(page: Mapping[str, Any]) -> List[str]:
    images = page.get("images")
    if not isinstance(images, Collection) or isinstance(images, (str, bytes, bytearray)):
        return []
    ids: List[str] = []
    for item in images:
        if not isinstance(item, Mapping):
            continue
        image_id = item.get("image_id")
        if isinstance(image_id, str) and image_id:
            ids.append(image_id)
    return ids
