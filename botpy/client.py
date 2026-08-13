import asyncio
import base64
import inspect
import re
import traceback
from collections import OrderedDict
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Coroutine, Dict, Iterable, List, Mapping, Optional, Tuple, Type, Union
from urllib.parse import unquote, urlparse
from weakref import WeakSet

from . import logging
from .api import BotAPI
from .configuration import ConfigurationManager, Menu, Panel
from .connection import ConnectionSession, ConnectionState
from .flags import Intents
from .gateway import BotWebSocket
from .http import BotHttp
from .middleware import Middleware, MiddlewareContext, create_middleware_context, run_middleware_chain
from .protocol.events import normalize_inbound_message
from .protocol.media import ChunkedMediaUploader, ProgressCallback
from .protocol.message import (
    LARGE_MEDIA_THRESHOLD,
    MAX_MEDIA_UPLOAD_SIZE,
    MEDIA_FILE_SIZE_LIMITS,
    MediaFileType,
    MediaSendResult,
    MessageType,
)
from .protocol.models import InboundMessage, InteractionContext, RawEvent, ReplyTarget
from .protocol.reply import ReplyLimiter
from .protocol.session import SessionStore
from .protocol.streaming import DEFAULT_STREAM_THROTTLE_MS, StreamSession
from .protocol.text import TEXT_CHUNK_LIMIT, chunk_text
from .protocol.transport import EventTransport, WebhookServerAdapter, WebhookTransport
from .protocol.upload_cache import UploadCache, compute_file_hash
from .robot import Robot, Token

_log = logging.get_logger()

_LEGACY_EVENT_CALLBACKS = {
    "group_member_add": "message_group_member_add",
    "group_member_remove": "message_group_member_remove",
}


class _LoopSentinel:
    __slots__ = ()

    def __getattr__(self, attr: str) -> None:
        raise AttributeError("无法在非异步上下文中访问循环属性")


_loop: Any = _LoopSentinel()


class Client:
    """``Client` 是一个用于与 QQ频道机器人 Websocket 和 API 交互的类。"""

    def __init__(
        self,
        intents: Intents,
        timeout: int = 5,
        is_sandbox=False,
        log_config: Union[str, dict] = None,
        log_format: str = None,
        log_level: int = None,
        bot_log: Union[bool, None] = True,
        ext_handlers: Union[dict, List[dict], bool] = True,
        session_store: Optional[SessionStore] = None,
        transport: Union[str, EventTransport] = "websocket",
        webhook_host: str = "0.0.0.0",
        webhook_port: int = 8080,
        webhook_path: str = "/",
        webhook_server: Optional[WebhookServerAdapter] = None,
        middlewares: Optional[Iterable[Middleware]] = None,
        markdown_support: bool = False,
        base_url: Optional[str] = None,
        token_base_url: str = "https://bots.qq.com",
        user_agent: str = "qq-botpy",
        ssl: Any = None,
        upload_cache: Optional[UploadCache] = None,
        reply_limiter: Optional[ReplyLimiter] = None,
        on_message_sent: Optional[Callable[[str, Mapping[str, Any]], Any]] = None,
        loguru_logger: Any = None,
        menu: Optional[Menu] = None,
        panels: Optional[Iterable[Panel]] = None,
        config_sync_strict: bool = False,
    ):
        """
        Args:
          intents (Intents): 通道：机器人需要注册的通道事件code，通过Intents提供的方法获取。
          timeout (int): 机器人 HTTP 请求的超时时间。. Defaults to 5
          is_sandbox: 是否使用沙盒环境。. Defaults to False

          log_config: 日志配置，可以为dict或.json/.yaml文件路径，会从文件中读取(logging.config.dictConfig)。Default to None（不做更改）
          log_format: 控制台输出格式(logging.basicConfig(format=))。Default to None（不做更改）
          log_level: 控制台输出level。Default to None(不做更改),
          bot_log: bot_log: bot_log: 是否启用bot日志 True/启用 None/禁用拓展 False/禁用拓展+控制台输出
          ext_handlers: ext_handlers: 额外的handler，格式参考 logging.DEFAULT_FILE_HANDLER。Default to True(使用默认追加handler)
          session_store: 可选的 Gateway Session 持久化适配器，用于进程重启后 Resume。
          transport: ``websocket``、``webhook`` 或自定义 EventTransport。
          middlewares: 依次处理统一入站消息的中间件。
          markdown_support: ``send_text`` 是否默认使用 Markdown 消息。
          base_url: 自定义开放平台 REST API 根地址，主要用于测试或代理。
          token_base_url: 自定义 access token 服务根地址。
          user_agent: HTTP 请求使用的 User-Agent。
          ssl: 传给 aiohttp 的 SSLContext、Fingerprint 或布尔值。
          upload_cache: 自定义媒体上传缓存；默认每个 Client 独享一份内存缓存。
          reply_limiter: 自定义被动回复限制器；默认每条消息每小时最多 4 次。
          on_message_sent: 平台响应包含 ``ext_info.ref_idx`` 时调用的出站消息钩子。
          loguru_logger: 可选的 Loguru logger；提供后 botpy 标准库日志会转发到该 logger。
          menu: 可选的声明式 C2C 全局菜单；配置后在登录成功时按差异同步。
          panels: 可选的声明式指令面板集合；使用稳定 key 非破坏性同步。
          config_sync_strict: 配置同步失败时是否中止客户端启动。
        """
        self.intents: int = intents.value
        self.ret_coro: bool = False
        # TODO loop的整体梳理 @veehou
        self._owns_loop = False
        try:
            self.loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self._owns_loop = True
        if not isinstance(markdown_support, bool):
            raise TypeError("markdown_support must be a bool")
        if not isinstance(config_sync_strict, bool):
            raise TypeError("config_sync_strict must be a bool")
        self._markdown_support = markdown_support
        self._token_base_url = token_base_url
        self._user_agent = user_agent
        self._ssl = ssl
        self.http: BotHttp = BotHttp(
            timeout=timeout,
            is_sandbox=is_sandbox,
            base_url=base_url,
            token_base_url=token_base_url,
            user_agent=user_agent,
            ssl=ssl,
        )
        self.api: BotAPI = BotAPI(http=self.http)
        self.configuration = ConfigurationManager(
            self.api,
            menu=menu,
            panels=tuple(panels or ()),
        )
        self._config_sync_strict = config_sync_strict

        self._connection: Optional[ConnectionSession] = None
        self._closed: bool = False
        self._listeners: Dict[str, List[Tuple[asyncio.Future, Callable[..., bool]]]] = {}
        self._ws_ap: Dict = {}
        self._websockets: set[BotWebSocket] = set()
        self._session_store = session_store
        self._robot: Optional[Robot] = None
        self._appid: Optional[str] = None
        self._middlewares = list(middlewares or ())
        self._reply_sequences: "OrderedDict[str, int]" = OrderedDict()
        self._reply_limiter = reply_limiter or ReplyLimiter()
        self._upload_cache = upload_cache or UploadCache(logger=_log)
        if on_message_sent is not None and not callable(on_message_sent):
            raise TypeError("on_message_sent must be callable")
        self._message_sent_hook = on_message_sent
        self._stream_sessions: "WeakSet[StreamSession]" = WeakSet()
        self._transport_state = ConnectionState(self.ws_dispatch, self.api)
        self._event_transport: Optional[EventTransport] = None
        if isinstance(transport, str):
            self._transport_mode = transport.lower()
            if self._transport_mode not in ("websocket", "webhook"):
                raise ValueError("transport must be 'websocket', 'webhook', or EventTransport")
        elif isinstance(transport, EventTransport):
            self._transport_mode = "custom"
            self._event_transport = transport
        else:
            raise TypeError("transport must be a string or EventTransport")
        self._webhook_host = webhook_host
        self._webhook_port = webhook_port
        self._webhook_path = webhook_path
        self._webhook_server = webhook_server

        logging.configure_logging(
            config=log_config,
            _format=log_format,
            level=log_level,
            bot_log=bot_log,
            ext_handlers=False if loguru_logger is not None and ext_handlers is True else ext_handlers,
        )
        if loguru_logger is not None:
            logging.configure_loguru(loguru_logger)

    async def __aenter__(self):
        _log.debug("[botpy] 机器人客户端: __aenter__")
        await self._async_setup_hook()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        _log.debug("[botpy] 机器人客户端: __aexit__")

        if not self.is_closed():
            await self.close()

    @property
    def robot(self):
        if self._robot is None:
            raise RuntimeError("机器人尚未登录")
        return self._robot

    def use(self, *middlewares: Middleware) -> "Client":
        """追加统一消息中间件，并返回当前 Client 以便链式配置。"""

        if self._closed:
            raise RuntimeError("无法向已关闭的 Client 添加中间件")
        if not all(callable(middleware) for middleware in middlewares):
            raise TypeError("middleware must be callable")
        self._middlewares.extend(middlewares)
        return self

    @property
    def middlewares(self) -> Tuple[Middleware, ...]:
        return tuple(self._middlewares)

    def set_message_sent_hook(
        self,
        callback: Optional[Callable[[str, Mapping[str, Any]], Any]],
    ) -> None:
        """设置收到出站 ``ref_idx`` 时的同步或异步回调。"""

        if callback is not None and not callable(callback):
            raise TypeError("callback must be callable")
        self._message_sent_hook = callback

    async def send(
        self,
        target: ReplyTarget,
        *,
        content: Optional[str] = None,
        msg_type: Optional[Union[int, MessageType]] = None,
        markdown: Optional[Mapping[str, Any]] = None,
        ark: Optional[Mapping[str, Any]] = None,
        embed: Optional[Mapping[str, Any]] = None,
        media: Optional[Mapping[str, Any]] = None,
        keyboard: Optional[Mapping[str, Any]] = None,
        message_reference: Optional[Mapping[str, Any]] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ):
        """统一发送文本、Markdown、Ark、Embed、媒体和键盘消息。

        C2C/群聊会自动推断 ``msg_type`` 并维护同一被动回复的 ``msg_seq``；
        频道和频道私信接口没有 ``msg_type`` 字段，会按具体消息字段直接发送。
        """

        if not isinstance(target, ReplyTarget):
            raise TypeError("target must be a ReplyTarget")
        if extra is not None and not isinstance(extra, Mapping):
            raise TypeError("extra must be a mapping")

        payload = {
            "content": content,
            "msg_type": int(msg_type) if msg_type is not None else None,
            "markdown": markdown,
            "ark": ark,
            "embed": embed,
            "media": media,
            "keyboard": keyboard,
            "message_reference": message_reference,
            "msg_id": target.message_id,
            "event_id": target.event_id,
        }
        if extra:
            payload.update(extra)
        payload = {key: value for key, value in payload.items() if value is not None}

        if target.scope in ("c2c", "group"):
            passive_message_id = payload.get("msg_id")
            if passive_message_id:
                limiter = getattr(self, "_reply_limiter", None)
                if limiter is None:
                    limiter = ReplyLimiter()
                    self._reply_limiter = limiter
                limit_result = limiter.check(passive_message_id)
                if not limit_result.allowed:
                    _log.info(
                        "[botpy] 被动回复已转为主动消息: msg_id=%s reason=%s",
                        passive_message_id,
                        limit_result.fallback_reason,
                    )
                    payload.pop("msg_id", None)
                    payload.pop("event_id", None)
                    payload.pop("msg_seq", None)
                    passive_message_id = None
            if "msg_type" not in payload:
                payload["msg_type"] = int(Client._infer_message_type(payload))
            if "msg_seq" not in payload and payload.get("msg_id"):
                payload["msg_seq"] = Client._next_reply_sequence(self, payload.get("msg_id"))
            method = self.api.post_c2c_message if target.scope == "c2c" else self.api.post_group_message
            result = await method(target.target_id, **payload)
            if passive_message_id:
                self._reply_limiter.record(passive_message_id)
            await Client._notify_message_sent(
                self,
                result,
                {"scope": target.scope, "target_id": target.target_id, "payload": dict(payload)},
            )
            return result

        if target.scope in ("channel", "dm"):
            if payload.get("media") is not None:
                raise ValueError("media references are only supported for c2c and group targets")
            payload.pop("msg_type", None)
            payload.pop("msg_seq", None)
            method = self.api.post_message if target.scope == "channel" else self.api.post_dms
            result = await method(target.target_id, **payload)
            await Client._notify_message_sent(
                self,
                result,
                {"scope": target.scope, "target_id": target.target_id, "payload": dict(payload)},
            )
            return result

        raise ValueError(f"unsupported reply target scope: {target.scope}")

    @staticmethod
    def _infer_message_type(payload: Mapping[str, Any]) -> MessageType:
        if payload.get("markdown") is not None:
            return MessageType.MARKDOWN
        if payload.get("ark") is not None:
            return MessageType.ARK
        if payload.get("embed") is not None:
            return MessageType.EMBED
        if payload.get("media") is not None:
            return MessageType.MEDIA
        return MessageType.TEXT

    async def send_text(self, target: ReplyTarget, content: str):
        """发送文本；超过 5000 字符时自动分段，开启 Markdown 权限时使用 Markdown。"""

        chunks = chunk_text(content, TEXT_CHUNK_LIMIT)
        results = []
        for chunk in chunks:
            if getattr(self, "_markdown_support", False):
                result = await Client.send_markdown(self, target, chunk)
            else:
                result = await Client.send(self, target, content=chunk, msg_type=MessageType.TEXT)
            results.append(result)
        return results[0] if len(results) == 1 else results

    async def send_markdown(
        self,
        target: ReplyTarget,
        content: str,
        keyboard: Optional[Mapping[str, Any]] = None,
    ):
        """发送原生 Markdown，可选附带内联键盘。"""

        return await Client.send(
            self,
            target,
            msg_type=MessageType.MARKDOWN,
            markdown={"content": content},
            keyboard=keyboard,
        )

    async def send_text_with_keyboard(
        self,
        target: ReplyTarget,
        content: str,
        keyboard: Mapping[str, Any],
    ):
        """发送带内联键盘的文本消息。"""

        return await Client.send(
            self,
            target,
            content=content,
            msg_type=MessageType.TEXT,
            keyboard=keyboard,
        )

    async def send_wakeup(self, target: ReplyTarget, content: str):
        """发送 C2C 唤醒消息；平台会继续执行 30 天窗口与频率限制。"""

        if target.scope != "c2c":
            raise ValueError("wakeup messages are only supported for c2c targets")
        return await Client.send(self, target, content=content, extra={"is_wakeup": True})

    async def recall_message(self, target: ReplyTarget, message_id: str, *, hidetip: bool = False):
        """按目标类型撤回消息；频道私信当前没有已确认的撤回协议。"""

        if target.scope == "c2c":
            return await self.api.recall_c2c_message(target.target_id, message_id)
        if target.scope == "group":
            return await self.api.recall_group_message(target.target_id, message_id)
        if target.scope == "channel":
            return await self.api.recall_message(target.target_id, message_id, hidetip=hidetip)
        if target.scope == "dm":
            raise ValueError("recalling dm messages is not supported by the confirmed API routes")
        raise ValueError(f"unsupported reply target scope: {target.scope}")

    async def upload_media(
        self,
        target: ReplyTarget,
        file_type: Union[int, MediaFileType],
        *,
        url: Optional[str] = None,
        file_data: Optional[str] = None,
        data: Optional[bytes] = None,
        local_path: Optional[Union[str, Path]] = None,
        file_name: Optional[str] = None,
        srv_send_msg: bool = False,
        on_progress: Optional[ProgressCallback] = None,
    ):
        """上传媒体；bytes/本地文件达到 5 MiB 时自动切换到分片协议。"""

        if target.scope not in ("c2c", "group"):
            raise ValueError("media upload is only supported for c2c and group targets")
        try:
            normalized_type = MediaFileType(file_type)
        except (TypeError, ValueError) as exc:
            raise ValueError("file_type must be a valid MediaFileType") from exc
        if isinstance(file_type, bool):
            raise ValueError("file_type must be a valid MediaFileType")

        sources = (url, file_data, data, local_path)
        if sum(source is not None for source in sources) != 1:
            raise ValueError("exactly one of url, file_data, data, or local_path is required")
        if on_progress is not None and not callable(on_progress):
            raise TypeError("on_progress must be callable")

        chunked_data = None
        chunked_path = None
        content_hash = None
        if url is not None:
            if not isinstance(url, str) or not url.strip():
                raise ValueError("url must be a non-empty string")
        elif file_data is not None:
            raw_file_data = Client._decode_base64_upload(file_data)
            content_hash = compute_file_hash(raw_file_data)
        elif data is not None:
            if not isinstance(data, bytes):
                raise TypeError("data must be bytes")
            size = len(data)
            Client._validate_media_size(normalized_type, size)
            content_hash = compute_file_hash(data)
            if size >= LARGE_MEDIA_THRESHOLD:
                chunked_data = data
            else:
                Client._validate_upload_size(size)
                file_data = base64.b64encode(data).decode("ascii")
        else:
            path = Path(local_path)
            if path.is_symlink() or not path.is_file():
                raise ValueError("local_path must point to a regular file and may not be a symlink")
            size = path.stat().st_size
            Client._validate_media_size(normalized_type, size)
            if file_name is None:
                file_name = path.name
            if size >= LARGE_MEDIA_THRESHOLD:
                chunked_path = path
            else:
                Client._validate_upload_size(size)
                raw = await asyncio.to_thread(path.read_bytes)
                Client._validate_upload_size(len(raw))
                content_hash = compute_file_hash(raw)
                file_data = base64.b64encode(raw).decode("ascii")

        upload_cache = getattr(self, "_upload_cache", None)
        if upload_cache is None:
            upload_cache = UploadCache(logger=_log)
            self._upload_cache = upload_cache
        if content_hash is not None:
            cached = upload_cache.get(content_hash, target.scope, target.target_id, normalized_type)
            if cached is not None:
                return {"file_uuid": "", "file_info": cached, "ttl": 0, "cached": True}

        if chunked_data is not None or chunked_path is not None:
            if srv_send_msg:
                raise ValueError("srv_send_msg is not supported by the confirmed chunked upload protocol")
            uploader = getattr(self, "_chunked_media_uploader", None)
            if uploader is None:
                uploader = ChunkedMediaUploader(self.api, logger=_log, upload_cache=upload_cache)
                self._chunked_media_uploader = uploader
            return await uploader.upload(
                target.scope,
                target.target_id,
                normalized_type,
                data=chunked_data,
                local_path=chunked_path,
                file_name=file_name,
                on_progress=on_progress,
            )

        if file_name is None and url and normalized_type == MediaFileType.FILE:
            file_name = unquote(Path(urlparse(url).path).name) or None
        if file_name is not None:
            file_name = Client._sanitize_media_file_name(file_name)

        method = self.api.post_c2c_file if target.scope == "c2c" else self.api.post_group_file
        result = await method(
            target.target_id,
            file_type=int(normalized_type),
            url=url,
            srv_send_msg=srv_send_msg,
            file_data=file_data,
            file_name=file_name if normalized_type == MediaFileType.FILE else None,
        )
        if content_hash is not None and isinstance(result, Mapping):
            upload_cache.store_response(content_hash, target.scope, target.target_id, normalized_type, result)
        return result

    async def send_media(
        self,
        target: ReplyTarget,
        file_type: Union[int, MediaFileType],
        *,
        url: Optional[str] = None,
        file_data: Optional[str] = None,
        data: Optional[bytes] = None,
        local_path: Optional[Union[str, Path]] = None,
        file_name: Optional[str] = None,
        content: Optional[str] = None,
        on_progress: Optional[ProgressCallback] = None,
    ) -> MediaSendResult:
        """上传媒体并用返回的 ``file_info`` 发送富媒体消息。"""

        upload = await Client.upload_media(
            self,
            target,
            file_type,
            url=url,
            file_data=file_data,
            data=data,
            local_path=local_path,
            file_name=file_name,
            srv_send_msg=False,
            on_progress=on_progress,
        )
        file_info = upload.get("file_info") if isinstance(upload, Mapping) else None
        if not file_info:
            raise RuntimeError("media upload response does not contain file_info")
        sent = await Client.send(
            self,
            target,
            content=content,
            msg_type=MessageType.MEDIA,
            media={"file_info": file_info},
        )
        return MediaSendResult(upload=upload, message=sent)

    async def send_image(self, target: ReplyTarget, **kwargs) -> MediaSendResult:
        """上传并发送图片。"""

        return await Client.send_media(self, target, MediaFileType.IMAGE, **kwargs)

    async def send_video(self, target: ReplyTarget, **kwargs) -> MediaSendResult:
        """上传并发送视频。"""

        return await Client.send_media(self, target, MediaFileType.VIDEO, **kwargs)

    async def send_voice(self, target: ReplyTarget, **kwargs) -> MediaSendResult:
        """上传并发送语音。"""

        return await Client.send_media(self, target, MediaFileType.VOICE, **kwargs)

    async def send_file(self, target: ReplyTarget, **kwargs) -> MediaSendResult:
        """上传并发送普通文件（需要机器人具备对应权限）。"""

        return await Client.send_media(self, target, MediaFileType.FILE, **kwargs)

    @staticmethod
    def _validate_upload_size(size: int) -> None:
        if size > MAX_MEDIA_UPLOAD_SIZE:
            raise ValueError("media is too large; the one-shot upload limit is 20 MiB")

    @staticmethod
    def _validate_media_size(file_type: MediaFileType, size: int) -> None:
        if size <= 0:
            raise ValueError("media file must not be empty")
        limit = MEDIA_FILE_SIZE_LIMITS[int(file_type)]
        if size > limit:
            raise ValueError(
                f"media is too large for {file_type.name.lower()}; limit is {limit // (1024 * 1024)} MiB"
            )

    @staticmethod
    def _validate_base64_upload(file_data: str) -> None:
        Client._decode_base64_upload(file_data)

    @staticmethod
    def _decode_base64_upload(file_data: str) -> bytes:
        if not isinstance(file_data, str) or not file_data:
            raise ValueError("file_data must be a non-empty base64 string")
        try:
            encoded = file_data.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("file_data must contain ASCII base64 data") from exc
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("file_data must be valid base64 data") from exc
        Client._validate_media_size(MediaFileType.FILE, len(decoded))
        Client._validate_upload_size(len(decoded))
        return decoded

    @staticmethod
    def _sanitize_media_file_name(file_name: str) -> str:
        if not isinstance(file_name, str):
            raise TypeError("file_name must be a string")
        cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", file_name)
        cleaned = " ".join(cleaned.split()).strip()
        return cleaned or "file"

    async def send_typing(self, target: ReplyTarget, duration_seconds: int = 60):
        """向 C2C 用户发送输入状态通知。"""

        if target.scope != "c2c":
            raise ValueError("typing indicator is only supported for c2c targets")
        sequence = Client._next_reply_sequence(self, target.message_id)
        result = await self.api.post_c2c_typing(
            target.target_id,
            input_second=duration_seconds,
            msg_id=target.message_id,
            msg_seq=sequence,
        )
        await Client._notify_message_sent(
            self,
            result,
            {"scope": "c2c", "target_id": target.target_id, "kind": "typing"},
        )
        return result

    async def _notify_message_sent(self, result: Any, meta: Mapping[str, Any]) -> None:
        hook = getattr(self, "_message_sent_hook", None)
        if hook is None or not isinstance(result, Mapping):
            return
        ext_info = result.get("ext_info")
        ref_idx = ext_info.get("ref_idx") if isinstance(ext_info, Mapping) else None
        if not isinstance(ref_idx, str) or not ref_idx:
            return
        try:
            hook_result = hook(ref_idx, meta)
            if inspect.isawaitable(hook_result):
                await hook_result
        except Exception as exc:
            _log.warning("[botpy] on_message_sent hook failed: %s", exc)

    async def acknowledge_interaction(
        self,
        interaction: Union[str, Any],
        code: int = 0,
        data: Optional[Mapping[str, Any]] = None,
    ):
        """确认按钮等 Interaction；可传 interaction id 或带 ``id`` 属性的对象。"""

        interaction_id = interaction if isinstance(interaction, str) else getattr(interaction, "id", None)
        if not isinstance(interaction_id, str) or not interaction_id:
            raise ValueError("interaction id is required")
        return await self.api.on_interaction_result(interaction_id, code, data)

    def open_stream(
        self,
        target: ReplyTarget,
        *,
        event_id: Optional[str] = None,
        throttle_ms: int = DEFAULT_STREAM_THROTTLE_MS,
    ) -> StreamSession:
        """为一条入站 C2C 消息创建 replace-mode 流式回复会话。"""

        if target.scope != "c2c":
            raise ValueError("streaming is only supported for c2c targets")
        if not target.message_id:
            raise ValueError("streaming requires target.message_id from the inbound message")
        if not isinstance(throttle_ms, int) or isinstance(throttle_ms, bool):
            raise TypeError("throttle_ms must be an integer")

        session = StreamSession(
            self.api,
            openid=target.target_id,
            msg_id=target.message_id,
            event_id=event_id or target.message_id,
            msg_seq=Client._next_reply_sequence(self, target.message_id),
            throttle_ms=throttle_ms,
            logger=_log,
        )
        sessions = getattr(self, "_stream_sessions", None)
        if sessions is None:
            sessions = WeakSet()
            self._stream_sessions = sessions
        sessions.add(session)
        return session

    def _next_reply_sequence(self, message_id: Optional[str]) -> int:
        if not message_id:
            return 1
        sequences = getattr(self, "_reply_sequences", None)
        if sequences is None:
            sequences = OrderedDict()
            self._reply_sequences = sequences
        sequence = sequences.get(message_id, 0) + 1
        if sequence > 65535:
            sequence = 1
        sequences[message_id] = sequence
        sequences.move_to_end(message_id)
        while len(sequences) > 10000:
            sequences.popitem(last=False)
        return sequence

    async def close(self) -> None:
        """关闭client相关的连接"""

        if self._closed:
            return

        self._closed = True

        event_transport = getattr(self, "_event_transport", None)
        if event_transport is not None:
            try:
                await event_transport.close()
            except Exception as exc:
                _log.warning("[botpy] 关闭事件传输时发生异常: %s", exc)

        for stream_session in tuple(getattr(self, "_stream_sessions", ())):
            stream_session.cancel()

        websocket_results = await asyncio.gather(
            *(websocket.close() for websocket in tuple(self._websockets)),
            return_exceptions=True,
        )
        self._websockets.clear()
        for result in websocket_results:
            if isinstance(result, BaseException):
                _log.warning("[botpy] 关闭 websocket 时发生异常: %s", result)

        session_store = getattr(self, "_session_store", None)
        if session_store is not None:
            try:
                await session_store.close()
            except Exception as exc:
                _log.warning("[botpy] 关闭 Gateway Session Store 失败: %s", exc)

        configuration = getattr(self, "configuration", None)
        if configuration is not None:
            await configuration.close()

        await self.http.close()
        upload_cache = getattr(self, "_upload_cache", None)
        if upload_cache is not None:
            upload_cache.clear()

    def is_closed(self) -> bool:
        return self._closed

    async def on_ready(self):
        pass

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        traceback.print_exc()

    async def on_message(self, message: InboundMessage) -> None:
        """接收跨 C2C、群聊、频道和私信统一后的消息。"""

    async def on_message_context(self, context: MiddlewareContext) -> None:
        """接收包含中间件 state 的统一消息上下文；默认转调 on_message。"""

        await self.on_message(context.message)

    async def on_raw_event(self, event: RawEvent) -> None:
        """接收所有 Gateway Dispatch 原始事件，包括 SDK 尚未识别的新事件。"""

    async def on_interaction_context(self, context: InteractionContext) -> None:
        """接收统一 Interaction 上下文；旧 ``on_interaction_create`` 回调仍然保留。"""

    async def _async_setup_hook(self) -> None:
        # Called whenever the client needs to initialise asyncio objects with a running loop
        self.loop = asyncio.get_running_loop()
        self._ready = asyncio.Event()

    def run(self, *args: Any, **kwargs: Any) -> None:
        """
        机器人服务开始执行

        注意:
          这个函数必须是最后一个调用的函数，因为它是阻塞的。这意味着事件的注册或在此函数调用之后调用的任何内容在它返回之前不会执行。
          如果想获取协程对象，可以使用`start`方法执行服务, 如:
        ```
        async with Client as c:
            c.start()
        ```
        """

        async def runner():
            async with self:
                await self.start(*args, **kwargs)

        try:
            self.loop.run_until_complete(runner())
        except KeyboardInterrupt:
            return
        finally:
            if self._owns_loop and not self.loop.is_closed():
                self.loop.close()

    async def start(self, appid: str, secret: str, ret_coro: bool = False) -> Optional[Coroutine]:
        """机器人开始执行

        参数
        ------------
        appid: :class:`str`
            机器人 appid
        secret: :class:`str`
            机器人 secret
        ret_coro: :class:`bool`
            是否需要返回协程对象
        """
        # login后再进行后面的操作
        token = Token(
            appid,
            secret,
            base_url=self._token_base_url,
            user_agent=self._user_agent,
            ssl=self._ssl,
        )
        self._appid = appid
        self.ret_coro = ret_coro

        if self.loop is _loop:
            await self._async_setup_hook()

        use_gateway = self._transport_mode == "websocket"
        await self._bot_login(token, use_gateway=use_gateway)
        if use_gateway:
            return await self._bot_init(token)

        if self._transport_mode == "webhook":
            self._event_transport = WebhookTransport(
                appid,
                secret,
                host=self._webhook_host,
                port=self._webhook_port,
                path=self._webhook_path,
                server=self._webhook_server,
                logger=_log,
                on_started=self._handle_transport_started,
                on_error=self._handle_transport_error,
            )
        return await self._transport_init()

    async def _bot_login(self, token: Token, *, use_gateway: bool = True) -> None:
        _log.info("[botpy] 登录机器人账号中...")

        user = await self.http.login(token)
        self._robot = Robot(user)
        self._transport_state.robot = self._robot

        if self.configuration.enabled:
            try:
                result = await self.configuration.sync()
                _log.info(
                    "[botpy] 声明式配置同步完成: menu_changed=%s, panels_created=%s, "
                    "panels_updated=%s, panel_targets_changed=%s",
                    result.menu_changed,
                    result.panels_created,
                    result.panels_updated,
                    result.panel_targets_changed,
                )
            except asyncio.CancelledError:
                await self.configuration.close()
                raise
            except Exception as exc:
                if self._config_sync_strict:
                    await self.http.close()
                    self._robot = None
                    self._transport_state.robot = None
                    raise
                _log.warning("[botpy] 声明式配置同步失败，客户端将继续启动: %s", exc)

        if not use_gateway:
            return

        # 通过api获取websocket链接
        self._ws_ap = await self.api.get_ws_url()

        # 实例一个session_pool
        self._connection = ConnectionSession(
            max_async=self._ws_ap["session_start_limit"]["max_concurrency"],
            connect=self.bot_connect,
            dispatch=self.ws_dispatch,
            loop=self.loop,
            api=self.api,
        )

        self._connection.state.robot = self._robot

    async def _transport_init(self) -> Optional[Coroutine]:
        if self._event_transport is None:
            raise RuntimeError("事件传输尚未初始化")
        coroutine = self._event_transport.start(self._dispatch_transport_event)
        if self.ret_coro:
            return coroutine
        await coroutine
        return None

    async def _handle_transport_started(self, info) -> None:
        _log.info("[botpy] Webhook 事件传输已启动: %s", info)
        self._schedule_event(self.on_ready, "on_ready")

    async def _handle_transport_error(self, error: BaseException) -> None:
        self._schedule_event(self.on_error, "on_webhook_error", "webhook", error)

    async def _dispatch_transport_event(self, event: RawEvent) -> None:
        parser = self._transport_state.parsers.get(event.event_type.lower())
        if parser is not None:
            parser(dict(event.raw))
        await self.ws_raw_dispatch(event)

    async def _bot_init(self, token):
        _log.info("[botpy] 程序启动...")
        # 每个机器人创建的连接数不能超过remaining剩余连接数
        if self._ws_ap["shards"] > self._ws_ap["session_start_limit"]["remaining"]:
            raise Exception("[botpy] 超出会话限制...")

        # 根据session限制建立链接
        # max_concurrency 表示每个 5 秒窗口内可同时启动的会话数。
        # ConnectionSession 会按该并发数分批，因此批次之间固定等待 5 秒。
        session_interval = 5

        # 根据限制建立分片的并发链接数
        _log.debug(f'[botpy] 会话间隔: {session_interval}, 分片: {self._ws_ap["shards"]}, 事件代码: {self.intents}')
        return await self._pool_init(token.bot_token(), session_interval)

    async def _pool_init(self, token, session_interval):
        def _loop_exception_handler(_loop, context):
            # first, handle with default handler
            _loop.default_exception_handler(context)

            exception = context.get("exception")
            if isinstance(exception, ZeroDivisionError):
                _loop.stop()

        for i in range(self._ws_ap["shards"]):
            session = await self._create_gateway_session(token, i, self._ws_ap["shards"])
            self._connection.add(session)

        loop = self._connection.loop
        loop.set_exception_handler(_loop_exception_handler)

        while not self._closed:
            _log.debug("[botpy] 会话循环检查...")
            try:
                # 返回协程对象，交由开发者自行调控
                coroutine = self._connection.multi_run(session_interval)
                if self.ret_coro:
                    return coroutine
                elif coroutine:
                    await coroutine
                else:
                    await self.close()
                    _log.info("[botpy] 服务意外停止!")
            except KeyboardInterrupt:
                _log.info("[botpy] 服务强行停止!")
                # cancel all tasks lingering

    async def _create_gateway_session(self, token: Token, shard_id: int, shard_count: int):
        session_id = ""
        last_seq = None
        if self._session_store is not None:
            try:
                saved = await self._session_store.load(token.app_id, shard_id)
            except Exception as exc:
                _log.warning("[botpy] 加载 Gateway Session 失败: %s", exc)
            else:
                if saved is not None and saved.shard_count == shard_count:
                    session_id = saved.session_id
                    last_seq = saved.sequence
                    _log.info("[botpy] 已恢复分片 %s 的 Gateway Session", shard_id)
                elif saved is not None:
                    try:
                        await self._session_store.clear(token.app_id, shard_id)
                    except Exception as exc:
                        _log.warning("[botpy] 清理过期 Gateway Session 失败: %s", exc)

        return {
            "session_id": session_id,
            "last_seq": last_seq,
            "intent": self.intents,
            "token": token,
            "url": self._ws_ap["url"],
            "shards": {"shard_id": shard_id, "shard_count": shard_count},
            "session_store": self._session_store,
        }

    async def bot_connect(self, session):
        """
        newConnect 启动一个新的连接，如果连接在监听过程中报错了，或者被远端关闭了链接，需要识别关闭的原因，能否继续 resume
        如果能够 resume，则往 sessionChan 中放入带有 sessionID 的 session
        如果不能，则清理掉 sessionID，将 session 放入 sessionChan 中
        session 的启动，交给 start 中的 for 循环执行，session 不自己递归进行重连，避免递归深度过深

        param session: session对象
        """
        _log.info("[botpy] 会话启动中...")

        client = BotWebSocket(session, self._connection)
        self._websockets.add(client)
        try:
            await client.start(self.ws_raw_dispatch)
        except (Exception, KeyboardInterrupt, SystemExit) as e:
            await client.on_error(e)
        finally:
            self._websockets.discard(client)

    async def ws_raw_dispatch(self, event: RawEvent) -> None:
        """分发新增的原始事件与统一消息回调。"""
        self._schedule_event(self.on_raw_event, "on_raw_event", event)
        if event.event_type.upper() == "INTERACTION_CREATE":
            context = InteractionContext(client=self, event=event)
            self._schedule_event(self.on_interaction_context, "on_interaction_context", context)
        message = normalize_inbound_message(event)
        if message is not None:
            callback = getattr(self, "_run_message_pipeline", self.on_message)
            self._schedule_event(callback, "on_message", message)

    async def _run_message_pipeline(self, message: InboundMessage) -> None:
        context = create_middleware_context(self, message, _log)

        async def dispatch_message(current_context, next_call) -> None:
            await self.on_message_context(current_context)

        await run_middleware_chain((*self._middlewares, dispatch_message), context)

    def ws_dispatch(self, event: str, *args: Any, **kwargs: Any) -> None:
        """分发ws的下行事件

        解析client类的on_event事件，进行对应的事件回调
        """
        _log.debug("[botpy] 调度事件: %s", event)
        method = "on_" + event

        if hasattr(self, method):
            coro = getattr(self, method)
            self._schedule_event(coro, method, *args, **kwargs)
        else:
            legacy_event = _LEGACY_EVENT_CALLBACKS.get(event)
            legacy_method = f"on_{legacy_event}" if legacy_event else None
            if legacy_method and hasattr(self, legacy_method):
                coro = getattr(self, legacy_method)
                self._schedule_event(coro, legacy_method, *args, **kwargs)
            else:
                _log.debug("[botpy] 事件: %s 未注册", event)


    def _schedule_event(
        self,
        coro: Callable[..., Coroutine[Any, Any, Any]],
        event_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Task:
        wrapped = self._run_event(coro, event_name, *args, **kwargs)
        # Schedules the task
        return self.loop.create_task(wrapped, name=f"[botpy] {event_name}")

    async def _run_event(
        self,
        coro: Callable[..., Coroutine[Any, Any, Any]],
        event_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        try:
            _log.debug("[botpy] _run_event")
            await coro(*args, **kwargs)
        except asyncio.CancelledError:
            pass
        except Exception:
            try:
                await self.on_error(event_name, *args, **kwargs)
            except asyncio.CancelledError:
                pass
