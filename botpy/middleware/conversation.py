import inspect
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Protocol, Union

from ..protocol.models import InboundAttachment
from .base import Middleware, MiddlewareContext, resolve_policy


@dataclass(frozen=True)
class HistoryEntry:
    sender_id: str
    content: str
    timestamp: int
    message_id: str
    sender_name: Optional[str] = None


class HistoryStore(Protocol):
    def append(self, group_key: str, entry: HistoryEntry, limit: int) -> Any: ...

    def list(self, group_key: str, limit: int) -> Any: ...


class MemoryHistoryStore:
    """按会话保存固定长度历史记录的内存 Store。"""

    def __init__(self) -> None:
        self._buffers: Dict[str, list[HistoryEntry]] = {}

    def append(self, group_key: str, entry: HistoryEntry, limit: int) -> None:
        buffer = self._buffers.setdefault(group_key, [])
        if any(existing.message_id == entry.message_id for existing in buffer):
            return
        buffer.append(entry)
        if len(buffer) > limit:
            del buffer[: len(buffer) - limit]

    def list(self, group_key: str, limit: int) -> list[HistoryEntry]:
        return list(self._buffers.get(group_key, ())[-limit:])

    def clear(self, group_key: str) -> None:
        self._buffers.pop(group_key, None)

    def __len__(self) -> int:
        return len(self._buffers)


@dataclass(frozen=True)
class RefEntry:
    message_id: str
    sender_id: str
    content: str
    timestamp: Optional[str] = None
    sender_name: Optional[str] = None
    author_is_bot: Optional[bool] = None
    scope: Optional[str] = None
    attachments: tuple[InboundAttachment, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)


class RefIndexStore(Protocol):
    def get(self, key: str) -> Any: ...

    def set(self, key: str, entry: RefEntry) -> Any: ...


class MemoryRefIndexStore:
    """带 LRU 淘汰的内存引用消息索引。"""

    def __init__(self, max_size: int = 500) -> None:
        if max_size < 1:
            raise ValueError("max_size must be positive")
        self.max_size = max_size
        self._entries: "OrderedDict[str, RefEntry]" = OrderedDict()

    def get(self, key: str) -> Optional[RefEntry]:
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def set(self, key: str, entry: RefEntry) -> None:
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_size:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


@dataclass(frozen=True)
class ResolvedQuote:
    ref_key: str
    source: str
    text: str
    entry: Optional[RefEntry] = None
    raw_content: Optional[str] = None
    attachments: tuple[InboundAttachment, ...] = ()


def history_buffer(
    *,
    limit: int = 50,
    store: Optional[HistoryStore] = None,
    record_on_skip: bool = True,
    group_key: Optional[Callable[[MiddlewareContext], Optional[str]]] = None,
) -> Middleware:
    """记录群聊最近消息，并把当前消息之前的历史写入 ``state['history']``。"""

    if limit < 1:
        raise ValueError("history limit must be positive")
    history_store = store if store is not None else MemoryHistoryStore()
    get_key = group_key or _default_history_key

    async def middleware(context: MiddlewareContext, next_call) -> None:
        key = get_key(context)
        if not key:
            await next_call()
            return
        effective_limit = int(resolve_policy(context, "group.history_limit", limit, 50))
        if effective_limit < 1:
            effective_limit = 1

        try:
            history = await _maybe_await(history_store.list(key, effective_limit))
        except Exception as exc:
            context.log.error("[botpy] 读取消息历史失败: %s", exc)
            history = []
        context.state["history"] = list(history or ())

        entry = HistoryEntry(
            sender_id=context.message.author_id or "",
            sender_name=context.message.author_name,
            content=context.message.content,
            timestamp=_parse_timestamp(context.message.timestamp),
            message_id=context.message.id,
        )
        if record_on_skip:
            await _append_history(history_store, key, entry, effective_limit, context)
        await next_call()
        if not record_on_skip and not context.stopped:
            await _append_history(history_store, key, entry, effective_limit, context)

    return middleware


def quote_ref(
    *,
    store: Optional[RefIndexStore] = None,
    max_size: int = 500,
    content_limit: int = 200,
    enrich_entry: Optional[Callable[[RefEntry, MiddlewareContext], Any]] = None,
    prefer_msg_elements: bool = True,
) -> Middleware:
    """记录 msg_idx 映射，并解析当前消息的 ref_msg_idx 引用。"""

    if content_limit < 0:
        raise ValueError("content_limit must be non-negative")
    ref_store = store if store is not None else MemoryRefIndexStore(max_size=max_size)

    async def middleware(context: MiddlewareContext, next_call) -> None:
        metadata = context.message.metadata
        key = str(metadata.get("msg_idx") or context.message.id)
        if key:
            entry = RefEntry(
                message_id=context.message.id,
                sender_id=context.message.author_id or "",
                sender_name=context.message.author_name,
                content=(context.message.content or "")[:content_limit],
                timestamp=context.message.timestamp,
                author_is_bot=context.message.author_is_bot,
                scope=context.reply_target.scope,
                attachments=tuple(context.message.attachments),
            )
            if enrich_entry is not None:
                entry = await _maybe_await(enrich_entry(entry, context))
            try:
                await _maybe_await(ref_store.set(key, entry))
            except Exception as exc:
                context.log.error("[botpy] 写入引用消息索引失败: %s", exc)

        ref_key = metadata.get("ref_msg_idx")
        if ref_key is not None:
            await _resolve_quote(
                context,
                ref_store,
                str(ref_key),
                prefer_msg_elements=prefer_msg_elements,
            )
        await next_call()

    return middleware


def envelope_formatter(
    *,
    history_limit: int = 5,
    include_quote: bool = True,
    include_sender: bool = True,
    formatter: Optional[Callable[[MiddlewareContext], Any]] = None,
) -> Middleware:
    """把发送者、引用、历史和当前消息组装为适合模型输入的结构化信封。"""

    if history_limit < 0:
        raise ValueError("history_limit must be non-negative")

    async def middleware(context: MiddlewareContext, next_call) -> None:
        if formatter is not None:
            envelope = await _maybe_await(formatter(context))
        else:
            envelope = _build_envelope(
                context,
                history_limit=history_limit,
                include_quote=include_quote,
                include_sender=include_sender,
            )
        context.state["envelope"] = str(envelope)
        await next_call()

    return middleware


def _default_history_key(context: MiddlewareContext) -> Optional[str]:
    return context.reply_target.target_id if context.reply_target.scope == "group" else None


async def _append_history(
    store: HistoryStore,
    key: str,
    entry: HistoryEntry,
    limit: int,
    context: MiddlewareContext,
) -> None:
    try:
        await _maybe_await(store.append(key, entry, limit))
    except Exception as exc:
        context.log.error("[botpy] 写入消息历史失败: %s", exc)


async def _resolve_quote(
    context: MiddlewareContext,
    store: RefIndexStore,
    ref_key: str,
    *,
    prefer_msg_elements: bool,
) -> None:
    try:
        entry = await _maybe_await(store.get(ref_key))
    except Exception as exc:
        context.log.error("[botpy] 读取引用消息索引失败: %s", exc)
        entry = None
    elements_quote = _quote_from_elements(context.message.metadata.get("msg_elements"))
    if elements_quote is not None and (prefer_msg_elements or entry is None):
        raw_content, attachments, text = elements_quote
        context.state["quote"] = ResolvedQuote(
            ref_key=ref_key,
            source="msg_elements",
            raw_content=raw_content,
            attachments=attachments,
            text=text,
        )
    elif entry is not None:
        context.state["quote"] = ResolvedQuote(
            ref_key=ref_key,
            source="store",
            entry=entry,
            attachments=entry.attachments,
            text=_entry_text(entry),
        )
    else:
        context.state["quote"] = ResolvedQuote(ref_key=ref_key, source="none", text="")


def _quote_from_elements(value: Any) -> Optional[tuple[str, tuple[InboundAttachment, ...], str]]:
    if not isinstance(value, list) or not value or not isinstance(value[0], Mapping):
        return None
    element = value[0]
    raw_content = str(element.get("content") or "")
    attachments = tuple(_parse_attachment(item) for item in element.get("attachments") or ())
    attachments = tuple(item for item in attachments if item is not None)
    if not raw_content and not attachments:
        return None
    return raw_content, attachments, _build_attachment_text(raw_content, attachments)


def _parse_attachment(value: Any) -> Optional[InboundAttachment]:
    if not isinstance(value, Mapping):
        return None
    return InboundAttachment(
        url=_optional_string(value.get("url")),
        filename=_optional_string(value.get("filename")),
        content_type=_optional_string(value.get("content_type")),
        asr_refer_text=_optional_string(value.get("asr_refer_text")),
        raw=dict(value),
    )


def _entry_text(entry: RefEntry) -> str:
    return _build_attachment_text(entry.content, entry.attachments)


def _build_attachment_text(content: str, attachments: Iterable[InboundAttachment]) -> str:
    parts = [content.strip()] if content.strip() else []
    parts.extend(_attachment_label(attachment) for attachment in attachments)
    return "\n".join(part for part in parts if part) or "[empty message]"


def _attachment_label(attachment: InboundAttachment) -> str:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("audio/"):
        return f"[voice: {attachment.asr_refer_text}]" if attachment.asr_refer_text else "[voice]"
    if content_type.startswith("image/"):
        return f"[image: {attachment.filename or 'image'}]"
    if content_type.startswith("video/"):
        return f"[video: {attachment.filename or 'video'}]"
    return f"[file: {attachment.filename or 'file'}]"


def _build_envelope(
    context: MiddlewareContext,
    *,
    history_limit: int,
    include_quote: bool,
    include_sender: bool,
) -> str:
    sections = []
    if include_sender:
        sender = context.message.author_name or context.message.author_id or "unknown"
        scope = context.reply_target.scope
        target = context.reply_target.target_id
        sections.append(f"<from>\nuser: {sender}\nscope: {scope}({target})\n</from>")

    quote = context.state.get("quote")
    if include_quote and isinstance(quote, ResolvedQuote) and quote.text:
        sender = None
        if quote.entry is not None:
            sender = quote.entry.sender_name or quote.entry.sender_id
        line = f"{sender}: {quote.text}" if sender else quote.text
        sections.append(f"<reply_to>\n{line}\n</reply_to>")

    history = context.state.get("history")
    if isinstance(history, list) and history_limit > 0:
        lines = []
        for entry in history[-history_limit:]:
            if isinstance(entry, HistoryEntry):
                sender = entry.sender_name or entry.sender_id
                lines.append(f"{sender}: {entry.content[:200]}")
        if lines:
            sections.append(f"<history>\n{'\n'.join(lines)}\n</history>")

    message_parts = []
    if context.message.content.strip():
        message_parts.append(context.message.content.strip())
    message_parts.extend(_attachment_label(attachment) for attachment in context.message.attachments)
    if message_parts:
        sections.append(f"<message>\n{'\n'.join(message_parts)}\n</message>")
    return "\n\n".join(sections)


def _parse_timestamp(value: Optional[str]) -> int:
    if value:
        try:
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            return int(datetime.fromisoformat(normalized).timestamp() * 1000)
        except ValueError:
            pass
    return time.time_ns() // 1_000_000


def _optional_string(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
