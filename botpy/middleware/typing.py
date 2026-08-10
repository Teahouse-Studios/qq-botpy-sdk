import asyncio
import inspect
from typing import Any, Callable, Optional

from .base import Middleware, MiddlewareContext


def typing_indicator(
    *,
    duration_seconds: int = 60,
    predicate: Optional[Callable[[MiddlewareContext], Any]] = None,
    await_typing: bool = False,
    keepalive: bool = True,
    keepalive_interval_seconds: float = 50,
) -> Middleware:
    """处理 C2C 消息时自动发送输入状态，并可周期续期。"""

    if duration_seconds < 1:
        raise ValueError("duration_seconds must be positive")
    if keepalive_interval_seconds <= 0:
        raise ValueError("keepalive_interval_seconds must be positive")
    pending_tasks: set[asyncio.Task] = set()

    async def middleware(context: MiddlewareContext, next_call) -> None:
        if context.reply_target.scope != "c2c":
            await next_call()
            return
        if predicate is not None and not bool(await _maybe_await(predicate(context))):
            await next_call()
            return

        initial = asyncio.create_task(_safe_send(context, duration_seconds))
        pending_tasks.add(initial)
        initial.add_done_callback(pending_tasks.discard)
        if await_typing:
            await initial
        stop_event = asyncio.Event()
        keepalive_task = None
        if keepalive:
            keepalive_task = asyncio.create_task(
                _keepalive_loop(
                    context,
                    duration_seconds,
                    keepalive_interval_seconds,
                    stop_event,
                )
            )
        try:
            await next_call()
        finally:
            stop_event.set()
            if keepalive_task is not None:
                await asyncio.gather(keepalive_task, return_exceptions=True)

    return middleware


async def _keepalive_loop(
    context: MiddlewareContext,
    duration_seconds: int,
    interval_seconds: float,
    stop_event: asyncio.Event,
) -> None:
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            await _safe_send(context, duration_seconds)


async def _safe_send(context: MiddlewareContext, duration_seconds: int) -> None:
    try:
        await context.client.send_typing(context.reply_target, duration_seconds)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        debug = getattr(context.log, "debug", None)
        if callable(debug):
            debug("[botpy] 发送输入状态失败: %s", exc)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
