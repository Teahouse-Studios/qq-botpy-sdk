"""QQ Bot 协议层基础能力。

该包提供与现有 :class:`botpy.Client` 解耦的认证、HTTP、事件模型和传输接口。
旧公开 API 会逐步委托到这里，以便在保持兼容的同时替换内部实现。
"""

from .auth import TokenManager
from .audio import detect_ffmpeg, ffmpeg_to_pcm, pcm_to_wav, should_transcode_voice, strip_amr_header
from .errors import (
    ApiError,
    AuthenticationError,
    BotPyError,
    RateLimitError,
    TransportError,
    UploadDailyLimitExceededError,
)
from .events import normalize_inbound_message, parse_gateway_event
from .http import ApiClient
from .formatting import format_duration, format_error_message, format_file_size
from .image import (
    DEFAULT_IMAGE_SIZE,
    ImageSize,
    extract_qqbot_image_size,
    format_qqbot_markdown_image,
    get_image_size_from_data_url,
    has_qqbot_image_size,
    parse_image_size,
)
from .media import ChunkedMediaUploader, ChunkedUploadProgress, ProgressCallback, UploadHashes
from .message import (
    CHUNKED_MEDIA_MAX_SIZE,
    LARGE_MEDIA_THRESHOLD,
    MAX_MEDIA_UPLOAD_SIZE,
    MEDIA_FILE_SIZE_LIMITS,
    MediaFileType,
    MediaSendResult,
    MessageType,
)
from .models import InboundAttachment, InboundMessage, InteractionContext, RawEvent, ReplyTarget, SessionState
from .media_utils import (
    DataUrl,
    clean_extension,
    detect_media_kind,
    guess_mime_type,
    is_audio_file,
    is_data_source,
    is_http_source,
    is_image_file,
    is_video_file,
    parse_data_url,
)
from .reply import ReplyLimitResult, ReplyLimiter
from .reconnect import CloseAction, ReconnectPolicy
from .session import FileSessionStore, JsonFileSessionStore, MemorySessionStore, SessionStore
from .streaming import (
    DEFAULT_STREAM_THROTTLE_MS,
    MIN_STREAM_THROTTLE_MS,
    StreamSession,
)
from .text import TEXT_CHUNK_LIMIT, chunk_text
from .target import ParsedTarget, looks_like_qqbot_target, normalize_target, parse_target
from .upload_cache import UploadCache, UploadCacheStats, compute_file_hash
from .transport import (
    AiohttpWebhookServer,
    EventHandler,
    EventTransport,
    WebhookRequest,
    WebhookResponse,
    WebhookServerAdapter,
    WebhookTransport,
    ed25519_sign,
    sign_validation_response,
    verify_webhook_signature,
)

__all__ = (
    "ApiClient",
    "ApiError",
    "AiohttpWebhookServer",
    "AuthenticationError",
    "BotPyError",
    "DataUrl",
    "DEFAULT_IMAGE_SIZE",
    "CHUNKED_MEDIA_MAX_SIZE",
    "ChunkedMediaUploader",
    "ChunkedUploadProgress",
    "CloseAction",
    "DEFAULT_STREAM_THROTTLE_MS",
    "EventHandler",
    "EventTransport",
    "FileSessionStore",
    "InboundAttachment",
    "InboundMessage",
    "ImageSize",
    "InteractionContext",
    "JsonFileSessionStore",
    "LARGE_MEDIA_THRESHOLD",
    "MemorySessionStore",
    "MIN_STREAM_THROTTLE_MS",
    "MAX_MEDIA_UPLOAD_SIZE",
    "MEDIA_FILE_SIZE_LIMITS",
    "MediaFileType",
    "MediaSendResult",
    "MessageType",
    "ProgressCallback",
    "ParsedTarget",
    "RateLimitError",
    "RawEvent",
    "ReconnectPolicy",
    "ReplyTarget",
    "ReplyLimitResult",
    "ReplyLimiter",
    "SessionState",
    "SessionStore",
    "StreamSession",
    "TokenManager",
    "TEXT_CHUNK_LIMIT",
    "TransportError",
    "UploadDailyLimitExceededError",
    "UploadHashes",
    "UploadCache",
    "UploadCacheStats",
    "WebhookRequest",
    "WebhookResponse",
    "WebhookServerAdapter",
    "WebhookTransport",
    "ed25519_sign",
    "chunk_text",
    "clean_extension",
    "compute_file_hash",
    "detect_ffmpeg",
    "detect_media_kind",
    "extract_qqbot_image_size",
    "ffmpeg_to_pcm",
    "format_duration",
    "format_error_message",
    "format_file_size",
    "format_qqbot_markdown_image",
    "get_image_size_from_data_url",
    "guess_mime_type",
    "has_qqbot_image_size",
    "is_audio_file",
    "is_data_source",
    "is_http_source",
    "is_image_file",
    "is_video_file",
    "looks_like_qqbot_target",
    "normalize_target",
    "parse_data_url",
    "parse_image_size",
    "parse_target",
    "pcm_to_wav",
    "should_transcode_voice",
    "strip_amr_header",
    "normalize_inbound_message",
    "parse_gateway_event",
    "sign_validation_response",
    "verify_webhook_signature",
)
