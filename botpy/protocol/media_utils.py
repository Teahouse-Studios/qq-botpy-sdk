import base64
import mimetypes
from dataclasses import dataclass
from pathlib import PurePath
from typing import Literal, Optional
from urllib.parse import unquote_to_bytes, urlsplit


MediaKind = Literal["image", "voice", "video", "file"]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv"}
AUDIO_EXTENSIONS = {
    ".silk",
    ".slk",
    ".slac",
    ".amr",
    ".wav",
    ".mp3",
    ".ogg",
    ".opus",
    ".aac",
    ".flac",
    ".m4a",
    ".wma",
    ".pcm",
}


@dataclass(frozen=True)
class DataUrl:
    mime_type: str
    data: bytes
    is_base64: bool


def clean_extension(source: str) -> str:
    path = urlsplit(source).path if "://" in source else source.split("?", 1)[0].split("#", 1)[0]
    return PurePath(path).suffix.lower()


def guess_mime_type(source: str, default: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(urlsplit(source).path if "://" in source else source)
    return guessed or default


def parse_data_url(value: str) -> Optional[DataUrl]:
    if not isinstance(value, str) or not value.startswith("data:") or "," not in value:
        return None
    header, encoded = value[5:].split(",", 1)
    parts = header.split(";")
    mime_type = parts[0] or "text/plain"
    is_base64 = any(part.lower() == "base64" for part in parts[1:])
    try:
        data = base64.b64decode(encoded, validate=True) if is_base64 else unquote_to_bytes(encoded)
    except (ValueError, base64.binascii.Error):
        return None
    return DataUrl(mime_type=mime_type, data=data, is_base64=is_base64)


def is_audio_file(source: str, mime_type: Optional[str] = None) -> bool:
    return bool(mime_type and (mime_type == "voice" or mime_type.startswith("audio/"))) or (
        clean_extension(source) in AUDIO_EXTENSIONS
    )


def is_image_file(source: str, mime_type: Optional[str] = None) -> bool:
    return bool(mime_type and mime_type.startswith("image/")) or clean_extension(source) in IMAGE_EXTENSIONS


def is_video_file(source: str, mime_type: Optional[str] = None) -> bool:
    return bool(mime_type and mime_type.startswith("video/")) or clean_extension(source) in VIDEO_EXTENSIONS


def detect_media_kind(source: str, mime_type: Optional[str] = None) -> MediaKind:
    if is_audio_file(source, mime_type):
        return "voice"
    if is_video_file(source, mime_type):
        return "video"
    if is_image_file(source, mime_type):
        return "image"
    return "file"


def is_http_source(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def is_data_source(source: str) -> bool:
    return source.startswith("data:")
