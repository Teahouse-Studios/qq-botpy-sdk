from typing import Any, NotRequired, Optional, TypedDict

from ..robot import Token


class ShardConfig(TypedDict):
    shard_id: int
    shard_count: int


class Session(TypedDict):
    session_id: str
    last_seq: Optional[int]
    intent: int
    token: Token
    url: str
    shards: ShardConfig
    reconnect_policy: NotRequired[Any]
    session_store: NotRequired[Any]
