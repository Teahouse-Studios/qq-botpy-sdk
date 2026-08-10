from typing import Any, Dict, Mapping, Optional, Tuple, cast

from .models import ChatScope, InboundAttachment, InboundMessage, RawEvent, ReplyTarget


_MESSAGE_SCOPES: Dict[str, Tuple[str, str]] = {
    "C2C_MESSAGE_CREATE": ("c2c", "user_openid"),
    "GROUP_AT_MESSAGE_CREATE": ("group", "group_openid"),
    "GROUP_MESSAGE_CREATE": ("group", "group_openid"),
    "AT_MESSAGE_CREATE": ("channel", "channel_id"),
    "MESSAGE_CREATE": ("channel", "channel_id"),
    "DIRECT_MESSAGE_CREATE": ("dm", "guild_id"),
}


def parse_gateway_event(payload: Mapping[str, Any]) -> RawEvent:
    """将 Gateway payload 转为与具体传输方式无关的原始事件。"""

    opcode = payload.get("op")
    sequence = payload.get("s")
    data = payload.get("d")
    event_id = payload.get("id")
    if event_id is None and isinstance(data, Mapping):
        event_id = data.get("event_id")

    return RawEvent(
        event_type=str(payload.get("t") or ""),
        data=data,
        sequence=sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else None,
        event_id=str(event_id) if event_id is not None else None,
        opcode=opcode if isinstance(opcode, int) and not isinstance(opcode, bool) else 0,
        raw=dict(payload),
    )


def normalize_inbound_message(event: RawEvent) -> Optional[InboundMessage]:
    """统一 C2C、群聊、频道和私信消息的公共字段。"""

    event_type = event.event_type.upper()
    scope_config = _MESSAGE_SCOPES.get(event_type)
    if scope_config is None or not isinstance(event.data, Mapping):
        return None

    scope, target_field = scope_config
    data = event.data
    author = data.get("author")
    if not isinstance(author, Mapping):
        author = {}

    message_id = _optional_string(data.get("id"))
    target_value = author.get(target_field) if scope == "c2c" else data.get(target_field)
    target_id = _optional_string(target_value)
    if not message_id or not target_id:
        return None

    if scope == "c2c":
        author_id = _optional_string(author.get("user_openid"))
    elif scope == "group":
        author_id = _optional_string(author.get("member_openid"))
    else:
        author_id = _optional_string(author.get("id"))

    metadata = _message_metadata(data)
    ref_msg_idx, msg_idx = _parse_reference_indices(data)
    if ref_msg_idx:
        metadata["ref_msg_idx"] = ref_msg_idx
    if msg_idx:
        metadata["msg_idx"] = msg_idx

    return InboundMessage(
        id=message_id,
        content=str(data.get("content") or ""),
        reply_target=ReplyTarget(
            scope=cast(ChatScope, scope),
            target_id=target_id,
            message_id=message_id,
            event_id=event.event_id,
        ),
        event_type=event_type,
        author_id=author_id,
        author_name=_optional_string(author.get("username")),
        author_is_bot=author.get("bot") if isinstance(author.get("bot"), bool) else None,
        attachments=_parse_attachments(data.get("attachments")),
        event_id=event.event_id,
        timestamp=_optional_string(data.get("timestamp")),
        raw=dict(data),
        metadata=metadata,
    )


def _parse_attachments(value: Any) -> list[InboundAttachment]:
    if not isinstance(value, list):
        return []

    attachments = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        attachments.append(
            InboundAttachment(
                url=_optional_string(item.get("url")),
                filename=_optional_string(item.get("filename")),
                content_type=_optional_string(item.get("content_type")),
                size=_optional_int(item.get("size")),
                height=_optional_int(item.get("height")),
                width=_optional_int(item.get("width")),
                voice_wav_url=_optional_string(item.get("voice_wav_url")),
                asr_refer_text=_optional_string(item.get("asr_refer_text")),
                raw=dict(item),
            )
        )
    return attachments


def _message_metadata(data: Mapping[str, Any]) -> Dict[str, Any]:
    keys = (
        "channel_id",
        "guild_id",
        "group_openid",
        "message_reference",
        "message_scene",
        "message_type",
        "mentions",
        "msg_elements",
        "msg_seq",
        "seq",
        "seq_in_channel",
        "src_guild_id",
    )
    return {key: data[key] for key in keys if data.get(key) is not None}


def _parse_reference_indices(data: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    ref_msg_idx = None
    msg_idx = None
    scene = data.get("message_scene")
    ext = scene.get("ext") if isinstance(scene, Mapping) else None
    if isinstance(ext, list):
        for entry in ext:
            if not isinstance(entry, str) or "=" not in entry:
                continue
            key, value = (part.strip() for part in entry.split("=", 1))
            if not value:
                continue
            if key == "ref_msg_idx":
                ref_msg_idx = value
            elif key == "msg_idx":
                msg_idx = value

    if data.get("message_type") == 103:
        elements = data.get("msg_elements")
        if isinstance(elements, list):
            for element in elements:
                if isinstance(element, Mapping) and element.get("msg_idx"):
                    ref_msg_idx = str(element["msg_idx"])
                    break
    return ref_msg_idx, msg_idx


def _optional_string(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


def _optional_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
