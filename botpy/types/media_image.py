# -*- coding: utf-8 -*-
from typing import List, Literal, TypedDict


MediaImageContentType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]

MEDIA_IMAGE_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
"""平台支持的素材图片 MIME 类型。"""

MEDIA_IMAGE_MAX_SIZE = 10 * 1024 * 1024
"""单张素材图片大小上限（10 MB）。"""

MEDIA_IMAGE_MAX_COUNT = 100
"""单个机器人可保存的有效素材图片数量上限。"""

MEDIA_IMAGE_QUOTA_EXCEEDED_CODE = 40035007
"""素材图片数量超过上限（100 张）时平台返回的 err_code。"""

MEDIA_IMAGE_NOT_FOUND_CODE = 40035008
"""删除或查询的图片不存在时平台返回的 err_code（并发删除时可直接忽略）。"""

MEDIA_IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
"""扩展名到 MIME 类型的映射；平台要求二者一致。"""


class _MediaImagePrepareOptionalFields(TypedDict, total=False):
    expires_in: int  # 预签名 URL 有效期（秒）


class MediaImagePrepareResponse(_MediaImagePrepareOptionalFields):
    upload_id: str  # 上传任务 ID，4 小时内有效
    presigned_url: str  # 预签名上传 URL，用 HTTP PUT 直传图片二进制


class _MediaImageCompleteOptionalFields(TypedDict, total=False):
    ttl: int  # 滑动有效期窗口（秒），默认 7776000
    preview_url: str  # 临时预览链接，短时效，请勿持久化


class MediaImageCompleteResponse(_MediaImageCompleteOptionalFields):
    image_id: str  # 图片 ID，用于查询与删除
    url: str  # Markdown 引用链接：qqbot-media://image/v1/<token>


class _MediaImageRecordOptionalFields(TypedDict, total=False):
    file_name: str  # 上传时的文件名
    preview_url: str  # 临时预览链接，短时效
    created_at: int  # 上传时间（Unix 秒）
    expires_at: int  # 过期时间（Unix 秒），每次被引用后自动顺延


class MediaImageRecord(_MediaImageRecordOptionalFields):
    image_id: str
    url: str


class _MediaImageListOptionalFields(TypedDict, total=False):
    next_cursor: str  # 下一页游标，为空表示末页
    total: int  # 当前有效图片总数


class MediaImageListResponse(_MediaImageListOptionalFields):
    images: List[MediaImageRecord]
