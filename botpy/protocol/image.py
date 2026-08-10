import re
import struct
from dataclasses import dataclass
from typing import Optional

from .media_utils import parse_data_url


@dataclass(frozen=True)
class ImageSize:
    width: int
    height: int


DEFAULT_IMAGE_SIZE = ImageSize(512, 512)


def parse_image_size(data: bytes) -> Optional[ImageSize]:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return ImageSize(*struct.unpack(">II", data[16:24]))
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        return ImageSize(*struct.unpack("<HH", data[6:10]))
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        offset = 2
        while offset + 9 <= len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            if marker in (0xC0, 0xC2):
                height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
                return ImageSize(width, height)
            if offset + 4 > len(data):
                break
            length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
            if length < 2:
                break
            offset += length + 2
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X":
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return ImageSize(width, height)
        if kind == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return ImageSize((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", data[26:30])
            return ImageSize(width & 0x3FFF, height & 0x3FFF)
    return None


def get_image_size_from_data_url(value: str) -> Optional[ImageSize]:
    parsed = parse_data_url(value)
    if parsed is None or not parsed.mime_type.startswith("image/"):
        return None
    return parse_image_size(parsed.data)


def format_qqbot_markdown_image(url: str, size: Optional[ImageSize] = None) -> str:
    dimensions = size or DEFAULT_IMAGE_SIZE
    return f"![#{dimensions.width}px #{dimensions.height}px]({url})"


def has_qqbot_image_size(markdown: str) -> bool:
    return re.search(r"!\[#\d+px\s+#\d+px\]", markdown) is not None


def extract_qqbot_image_size(markdown: str) -> Optional[ImageSize]:
    match = re.search(r"!\[#(\d+)px\s+#(\d+)px\]", markdown)
    return ImageSize(int(match.group(1)), int(match.group(2))) if match else None
