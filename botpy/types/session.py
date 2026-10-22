from typing import Any, Optional, TypedDict

from ..robot import Token


class ShardConfig(TypedDict):
    shard_id: int
    shard_count: int


class _SessionOptionalFields(TypedDict, total=False):
    ssl: Any
    proxy: Any
    reconnect_policy: Any
    session_store: Any


class Session(_SessionOptionalFields):
    session_id: str
    last_seq: Optional[int]
    intent: int
    token: Token
    url: str
    shards: ShardConfig
