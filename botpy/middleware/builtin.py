import base64
import inspect
import json
import re
import time
from collections import OrderedDict
from dataclasses import replace
from typing import Any, Callable, Optional

from .base import Middleware, MiddlewareContext


_MENTION_RE = re.compile(r"<@!?\d+>\s*")
_OLD_FACE_RE = re.compile(r"\[<face,id=(\d+)/?>]")
_NEW_FACE_RE = re.compile(r'<faceType=\d+,faceId="[^"]*",ext="([^"]*)">')
_FACE_EMOJI = {
    "0": "😊",
    "1": "😣",
    "2": "😍",
    "4": "😎",
    "5": "😭",
    "8": "😴",
    "11": "😡",
    "16": "👍",
    "25": "🤔",
    "30": "💪",
    "32": "🎉",
    "53": "🎂",
    "63": "🌹",
    "66": "❤️",
    "76": "👏",
    "79": "✌️",
    "100": "😂",
}


def message_filter(
    *,
    skip_self_echo: bool = True,
    deduplicate: bool = True,
    window_seconds: float = 5.0,
    max_size: int = 1000,
) -> Middleware:
    """过滤机器人自身消息，并在时间窗口内按消息 ID 去重。"""

    if window_seconds < 0:
        raise ValueError("window_seconds must be non-negative")
    if max_size < 1:
        raise ValueError("max_size must be positive")

    seen: "OrderedDict[str, float]" = OrderedDict()

    async def middleware(context: MiddlewareContext, next_call) -> None:
        if skip_self_echo and context.message.author_is_bot is True:
            context.stop("self-echo")
            return

        if deduplicate:
            now = time.monotonic()
            message_id = context.message.id
            previous = seen.get(message_id)
            if previous is not None and now - previous <= window_seconds:
                context.stop("deduplication")
                return

            seen[message_id] = now
            seen.move_to_end(message_id)
            while seen:
                oldest_id, timestamp = next(iter(seen.items()))
                if len(seen) <= max_size and now - timestamp <= window_seconds:
                    break
                seen.pop(oldest_id)

        await next_call()

    return middleware


def content_sanitizer(
    *,
    strip_bot_mention: bool = True,
    strip_all_mentions: bool = False,
    collapse_whitespace: bool = False,
    parse_face_tags: bool = False,
    bot_id: Optional[str] = None,
    transform: Optional[Callable[[str, MiddlewareContext], str]] = None,
) -> Middleware:
    """清理 QQ mention、表情标签和多余空白，并允许自定义文本转换。"""

    async def middleware(context: MiddlewareContext, next_call) -> None:
        content = context.message.content or ""

        if strip_all_mentions:
            content = _MENTION_RE.sub("", content)
        elif strip_bot_mention:
            resolved_bot_id = bot_id or getattr(context.client, "_appid", None)
            if resolved_bot_id:
                mention = re.compile(rf"<@!?{re.escape(str(resolved_bot_id))}>\s*")
                content = mention.sub("", content)

        if parse_face_tags:
            content = _OLD_FACE_RE.sub(lambda match: _FACE_EMOJI.get(match.group(1), ""), content)
            content = _NEW_FACE_RE.sub(_decode_face_tag, content)
        else:
            content = _OLD_FACE_RE.sub("", content)
            content = _NEW_FACE_RE.sub("", content)

        if collapse_whitespace:
            content = re.sub(r"\s+", " ", content)
        content = content.strip()
        if transform is not None:
            content = transform(content, context)

        context.message = replace(context.message, content=content)
        await next_call()

    return middleware


def error_handler(
    handler: Optional[Callable[[Exception, MiddlewareContext], Any]] = None,
    *,
    rethrow: bool = False,
    predicate: Optional[Callable[[Exception], bool]] = None,
) -> Middleware:
    """捕获下游异常；可记录、自定义处理，并选择是否继续抛出。"""

    async def middleware(context: MiddlewareContext, next_call) -> None:
        try:
            await next_call()
        except Exception as exc:
            if predicate is not None and not predicate(exc):
                raise

            context.logger.error("[botpy] 消息中间件处理失败: %s", exc)
            if handler is not None:
                try:
                    result = handler(exc, context)
                    if inspect.isawaitable(result):
                        await result
                except Exception as handler_error:
                    context.logger.error("[botpy] 中间件错误处理器执行失败: %s", handler_error)
            if rethrow:
                raise

    return middleware


def _decode_face_tag(match: re.Match) -> str:
    try:
        decoded = base64.b64decode(match.group(1)).decode("utf-8")
        payload = json.loads(decoded)
        return f"【表情: {payload.get('text') or '未知表情'}】"
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return match.group(0)
