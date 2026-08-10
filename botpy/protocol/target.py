import re
from dataclasses import dataclass
from typing import Literal, Optional

from .models import ReplyTarget


TargetType = Literal["c2c", "group", "channel"]


@dataclass(frozen=True)
class ParsedTarget:
    type: TargetType
    id: str

    def to_reply_target(self) -> ReplyTarget:
        return ReplyTarget(scope=self.type, target_id=self.id)


def parse_target(value: str) -> ParsedTarget:
    if not isinstance(value, str):
        raise TypeError("target must be a string")
    normalized = re.sub(r"^qqbot:", "", value, flags=re.IGNORECASE)
    for target_type in ("c2c", "group", "channel"):
        prefix = f"{target_type}:"
        if normalized.lower().startswith(prefix):
            target_id = normalized[len(prefix) :]
            if not target_id:
                raise ValueError(f"invalid {target_type} target: missing id")
            return ParsedTarget(target_type, target_id)
    if not normalized:
        raise ValueError("invalid target: missing id")
    return ParsedTarget("c2c", normalized)


def normalize_target(value: str) -> Optional[str]:
    normalized = re.sub(r"^qqbot:", "", value, flags=re.IGNORECASE)
    if re.match(r"^(c2c|group|channel):.+$", normalized, flags=re.IGNORECASE):
        return f"qqbot:{normalized}"
    if re.fullmatch(r"[0-9a-fA-F]{32}", normalized) or re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", normalized
    ):
        return f"qqbot:c2c:{normalized}"
    return None


def looks_like_qqbot_target(value: str) -> bool:
    return normalize_target(value) is not None or bool(
        re.match(r"^(?:qqbot:)?(?:c2c|group|channel):", value, flags=re.IGNORECASE)
    )
