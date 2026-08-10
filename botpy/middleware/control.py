import asyncio
import inspect
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Deque, Dict, Literal, Optional

from .base import Middleware, MiddlewareContext


@dataclass(frozen=True)
class RateLimitTier:
    max_requests: int
    window_seconds: float

    def __post_init__(self) -> None:
        if self.max_requests < 1:
            raise ValueError("max_requests must be positive")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")


class _SlidingWindow:
    def __init__(self, tier: RateLimitTier) -> None:
        self.tier = tier
        self.buckets: Dict[str, Deque[float]] = defaultdict(deque)

    def check(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self.buckets[key]
        while bucket and now - bucket[0] > self.tier.window_seconds:
            bucket.popleft()
        if not bucket:
            self.buckets.pop(key, None)
            bucket = self.buckets[key]
        if len(bucket) >= self.tier.max_requests:
            return False
        bucket.append(now)
        return True


def rate_limiter(
    *,
    per_sender: Optional[RateLimitTier] = None,
    per_group: Optional[RateLimitTier] = None,
    global_limit: Optional[RateLimitTier] = None,
    on_limit: Optional[Callable[[MiddlewareContext, str], Any]] = None,
) -> Middleware:
    """基于滑动窗口提供发送者、会话和全局三级限流。"""

    sender_window = _SlidingWindow(per_sender) if per_sender else None
    group_window = _SlidingWindow(per_group) if per_group else None
    global_window = _SlidingWindow(global_limit) if global_limit else None

    async def middleware(context: MiddlewareContext, next_call) -> None:
        sender_id = context.message.author_id or ""
        scope = context.reply_target.scope
        group_key = context.reply_target.target_id if scope in ("group", "channel") else sender_id
        checks = (
            ("global", global_window, "__global__"),
            ("per_group", group_window, group_key),
            ("per_sender", sender_window, sender_id),
        )
        for name, window, key in checks:
            if window is not None and not window.check(key):
                if on_limit is not None:
                    await _maybe_await(on_limit(context, name))
                context.stop(f"rate-limit:{name}")
                return
        await next_call()

    return middleware


ConcurrencyStrategy = Literal["queue", "drop", "abort", "merge"]


@dataclass
class _Waiter:
    context: MiddlewareContext
    future: asyncio.Future


@dataclass
class _TargetState:
    busy: bool = False
    active_context: Optional[MiddlewareContext] = None
    queue: Deque[_Waiter] = field(default_factory=deque)
    merge_buffer: list[_Waiter] = field(default_factory=list)


def concurrency_guard(
    *,
    strategy: ConcurrencyStrategy = "queue",
    max_queue: int = 3,
    max_processing_seconds: float = 0,
    on_drop: Optional[Callable[[MiddlewareContext], Any]] = None,
    on_merge: Optional[Callable[[list[MiddlewareContext]], MiddlewareContext]] = None,
    urgent_predicate: Optional[Callable[[MiddlewareContext], bool]] = None,
) -> Middleware:
    """按 ReplyTarget 串行处理消息，支持排队、丢弃、中止和合并策略。"""

    if strategy not in ("queue", "drop", "abort", "merge"):
        raise ValueError("unknown concurrency strategy")
    if max_queue < 0:
        raise ValueError("max_queue must be non-negative")
    if max_processing_seconds < 0:
        raise ValueError("max_processing_seconds must be non-negative")

    states: Dict[str, _TargetState] = {}

    async def middleware(context: MiddlewareContext, next_call) -> None:
        key = f"{context.reply_target.scope}:{context.reply_target.target_id}"
        state = states.setdefault(key, _TargetState())

        if state.busy:
            disposition = await _handle_busy(
                state,
                context,
                strategy,
                max_queue,
                on_drop,
                urgent_predicate,
            )
            if disposition == "skip":
                return
            if disposition == "parallel":
                await next_call()
                return
        else:
            state.busy = True

        state.active_context = context
        try:
            if max_processing_seconds > 0:
                try:
                    await asyncio.wait_for(next_call(), timeout=max_processing_seconds)
                except TimeoutError:
                    context.abort("concurrency:processing-timeout")
            else:
                await next_call()
        finally:
            state.active_context = None
            _release_next(states, key, state, strategy, on_merge)

    return middleware


async def _handle_busy(
    state: _TargetState,
    context: MiddlewareContext,
    strategy: ConcurrencyStrategy,
    max_queue: int,
    on_drop,
    urgent_predicate,
) -> Literal["owner", "parallel", "skip"]:
    if strategy == "drop":
        await _drop(context, on_drop, "concurrency:drop")
        return "skip"

    if strategy == "abort":
        if state.active_context is not None:
            state.active_context.abort("concurrency:abort")
        while state.queue:
            waiter = state.queue.popleft()
            waiter.context.abort("concurrency:superseded")
            if not waiter.future.done():
                waiter.future.set_result(False)

    if strategy == "merge" and urgent_predicate is not None and urgent_predicate(context):
        for waiter in state.merge_buffer:
            if not waiter.future.done():
                waiter.future.set_result(False)
        state.merge_buffer.clear()
        return "parallel"

    target_queue = state.merge_buffer if strategy == "merge" else state.queue
    if len(target_queue) >= max_queue:
        suffix = "merge-full" if strategy == "merge" else "queue-full"
        await _drop(context, on_drop, f"concurrency:{suffix}")
        return "skip"

    future = asyncio.get_running_loop().create_future()
    waiter = _Waiter(context=context, future=future)
    target_queue.append(waiter)
    try:
        return "owner" if bool(await future) else "skip"
    except asyncio.CancelledError:
        try:
            target_queue.remove(waiter)
        except ValueError:
            pass
        raise


def _release_next(
    states: Dict[str, _TargetState],
    key: str,
    state: _TargetState,
    strategy: ConcurrencyStrategy,
    on_merge,
) -> None:
    if strategy == "merge" and state.merge_buffer:
        batch = state.merge_buffer[:]
        state.merge_buffer.clear()
        contexts = [waiter.context for waiter in batch]
        try:
            survivor = on_merge(contexts) if on_merge is not None else _default_merge(contexts)
        except Exception as exc:
            contexts[0].log.error("[botpy] 合并并发消息失败: %s", exc)
            survivor = _default_merge(contexts)
        if survivor not in contexts:
            survivor = contexts[0]
        for waiter in batch:
            if not waiter.future.done():
                waiter.future.set_result(waiter.context is survivor)
        state.busy = True
        return

    while state.queue:
        waiter = state.queue.popleft()
        if not waiter.future.done():
            waiter.future.set_result(True)
            state.busy = True
            return
    state.busy = False
    states.pop(key, None)


def _default_merge(contexts: list[MiddlewareContext]) -> MiddlewareContext:
    first = contexts[0]
    content_contexts = [context for context in contexts if context.message.content]
    if content_contexts:
        content = "\n".join(context.message.content for context in content_contexts)
        attachments = [
            attachment
            for context in content_contexts
            for attachment in context.message.attachments
        ]
        first.message = replace(first.message, content=content, attachments=attachments)
    return first


async def _drop(context: MiddlewareContext, callback, reason: str) -> None:
    if callback is not None:
        await _maybe_await(callback(context))
    context.stop(reason)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
