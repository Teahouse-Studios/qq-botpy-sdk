import json
from typing import Any


def format_error_message(error: Any) -> str:
    """格式化异常并遍历 ``__cause__`` 链。"""

    if isinstance(error, BaseException):
        parts = [str(error) or error.__class__.__name__]
        seen = {id(error)}
        cause = error.__cause__
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            parts.append(str(cause) or cause.__class__.__name__)
            cause = cause.__cause__
        return " | ".join(parts)
    if isinstance(error, str):
        return error
    try:
        return json.dumps(error, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(error)


def format_duration(milliseconds: float) -> str:
    seconds = round(milliseconds / 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining = divmod(seconds, 60)
    return f"{minutes}m {remaining}s" if remaining else f"{minutes}m"


def format_file_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.2f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.2f} MB"
    return f"{size / 1024**3:.2f} GB"
