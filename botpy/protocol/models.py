import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Mapping, Optional


ChatScope = Literal["c2c", "group", "channel", "dm"]


@dataclass(frozen=True)
class ReplyTarget:
    scope: ChatScope
    target_id: str
    message_id: Optional[str] = None
    event_id: Optional[str] = None


@dataclass(frozen=True)
class SessionState:
    session_id: str
    sequence: Optional[int]
    shard_id: int = 0
    shard_count: int = 1


@dataclass(frozen=True)
class RawEvent:
    event_type: str
    data: Any
    sequence: Optional[int] = None
    event_id: Optional[str] = None
    opcode: int = 0
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class InteractionContext:
    client: Any
    event: RawEvent
    state: Dict[str, Any] = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class InboundAttachment:
    url: Optional[str] = None
    filename: Optional[str] = None
    content_type: Optional[str] = None
    size: Optional[int] = None
    height: Optional[int] = None
    width: Optional[int] = None
    voice_wav_url: Optional[str] = None
    asr_refer_text: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InboundMessage:
    id: str
    content: str
    reply_target: ReplyTarget
    event_type: str
    author_id: Optional[str] = None
    author_name: Optional[str] = None
    author_is_bot: Optional[bool] = None
    attachments: List[InboundAttachment] = field(default_factory=list)
    event_id: Optional[str] = None
    timestamp: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
