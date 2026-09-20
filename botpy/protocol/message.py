from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Mapping


MAX_MEDIA_UPLOAD_SIZE = 20 * 1024 * 1024
LARGE_MEDIA_THRESHOLD = 5 * 1024 * 1024
CHUNKED_MEDIA_MAX_SIZE = 100 * 1024 * 1024


MEDIA_FILE_SIZE_LIMITS = {
    1: 30 * 1024 * 1024,
    2: CHUNKED_MEDIA_MAX_SIZE,
    3: MAX_MEDIA_UPLOAD_SIZE,
    4: CHUNKED_MEDIA_MAX_SIZE,
}


class MessageType(IntEnum):
    """QQ 开放平台 ``msg_type``。"""

    TEXT = 0
    MARKDOWN = 2
    ARK = 3
    EMBED = 4
    MEDIA = 7


class MediaFileType(IntEnum):
    """QQ 开放平台媒体上传 ``file_type``。"""

    IMAGE = 1
    VIDEO = 2
    VOICE = 3
    FILE = 4


@dataclass(frozen=True)
class MediaSendResult:
    """一次“上传并发送”操作的两个阶段结果。"""

    upload: Mapping[str, Any]
    message: Any


@dataclass(frozen=True)
class MediaUrlResult:
    """一次“上传并获取临时直链”操作的结果。

    ``raw_url`` 是平台返回的临时直链，可直接用于 Markdown 图片等内容；
    ``ttl`` 为剩余有效秒数，``0`` 表示平台未给出有效期。
    """

    upload: Mapping[str, Any]
    raw_url: str
    ttl: int
