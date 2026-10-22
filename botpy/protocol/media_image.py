# -*- coding: utf-8 -*-
"""素材管理（media image）上传流程。

平台要求先把 Markdown 消息要用的图片上传换取 ``qqbot-media://`` 引用：

1. ``POST /v2/media-images/prepare`` 申请上传，得到预签名 URL；
2. 用 HTTP ``PUT`` 把图片二进制直传到预签名 URL；
3. ``POST /v2/media-images/complete`` 完成上传，得到 ``image_id`` 与引用链接。

本模块把这三步串起来，并负责校验格式、大小与计算摘要。
"""

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Tuple, Union

from .. import logging
from ..types.media_image import MEDIA_IMAGE_EXTENSIONS, MEDIA_IMAGE_MAX_SIZE
from .image import ImageSize, parse_image_size


class MediaImageUploadApi(Protocol):
    """``MediaImageUploader`` 需要的最小 API 集合，便于测试替换。"""

    async def post_media_image_prepare(self, **payload: Any) -> Mapping[str, Any]: ...

    async def put_media_image(self, presigned_url: str, data: bytes, **kwargs: Any) -> Any: ...

    async def post_media_image_complete(self, **payload: Any) -> Mapping[str, Any]: ...


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


class MediaImageUploader:
    """执行素材图片的 prepare → PUT → complete 三步上传。"""

    def __init__(self, api: MediaImageUploadApi, *, logger: Any = None) -> None:
        self.api = api
        self.logger = logger or logging.get_logger()

    async def upload(
        self,
        *,
        data: Optional[bytes] = None,
        local_path: Optional[Union[str, Path]] = None,
        file_name: Optional[str] = None,
        content_type: Optional[str] = None,
        alt: Optional[str] = None,
        timeout: float = 300,
    ) -> MediaImageUploadResult:
        """上传一张图片并返回引用链接。

        ``data`` 与 ``local_path`` 必须二选一；``content_type`` 省略时按扩展名推断。
        ``alt`` 是 Markdown 图片的替代文本，省略时用文件名（不含扩展名）。
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
        prepared = await self.api.post_media_image_prepare(
            file_name=display_name,
            file_size=file_size,
            content_type=content_type,
            md5=md5,
            sha1=sha1,
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
        completed = await self.api.post_media_image_complete(upload_id=upload_id, md5=md5)
        image_url = completed.get("url")
        if not image_url:
            raise RuntimeError("complete 响应缺少 url")

        size = parse_image_size(payload)
        markdown = format_media_image_markdown(
            image_url,
            alt=alt if alt is not None else Path(display_name).stem,
            size=size,
        )
        return MediaImageUploadResult(
            image_id=str(completed.get("image_id") or ""),
            url=image_url,
            ttl=int(completed.get("ttl") or 0),
            preview_url=completed.get("preview_url"),
            upload_id=str(upload_id),
            file_name=display_name,
            content_type=content_type,
            size=file_size,
            markdown=markdown,
        )
