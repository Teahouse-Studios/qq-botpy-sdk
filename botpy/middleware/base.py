import asyncio
import inspect
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, TypeVar, Union

from ..protocol.models import InboundMessage, ReplyTarget


NextCallable = Callable[[], Awaitable[None]]
MiddlewareResult = Union[Awaitable[None], None]
Middleware = Callable[["MiddlewareContext", NextCallable], MiddlewareResult]
T = TypeVar("T")


class MiddlewareContext:
    """单条统一入站消息在中间件管线中的上下文。"""

    def __init__(self, client: Any, message: InboundMessage, logger: Any) -> None:
        self.client = client
        self.message = message
        self.state: Dict[str, Any] = {}
        self.logger = logger
        self.received_at = time.time_ns() // 1_000_000
        self._stopped = False
        self._stop_reason: Optional[str] = None
        self._abort_event = asyncio.Event()

    @property
    def reply_target(self) -> ReplyTarget:
        return self.message.reply_target

    @property
    def bot(self) -> Any:
        """当前 Client 的别名，便于中间件调用机器人 API。"""

        return self.client

    @property
    def log(self) -> Any:
        return self.logger

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def stop_reason(self) -> Optional[str]:
        return self._stop_reason

    @property
    def abort_event(self) -> asyncio.Event:
        """中止时被置位，耗时业务可以监听它来停止工作。"""

        return self._abort_event

    @property
    def aborted(self) -> bool:
        return self._abort_event.is_set()

    def stop(self, reason: Optional[str] = None) -> None:
        self._stopped = True
        self._stop_reason = reason

    def abort(self, reason: Optional[str] = None) -> None:
        self._abort_event.set()
        self.stop(reason or "aborted")


def create_middleware_context(client: Any, message: InboundMessage, logger: Any) -> MiddlewareContext:
    return MiddlewareContext(client=client, message=message, logger=logger)


async def run_middleware_chain(
    middlewares: Iterable[Middleware],
    context: MiddlewareContext,
) -> bool:
    """按 Koa 风格运行中间件，并阻止同一个 next() 被重复调用。"""

    chain = tuple(middlewares)
    index = -1

    async def dispatch(position: int) -> None:
        nonlocal index
        if position <= index:
            raise RuntimeError("next() called multiple times")
        index = position
        if context.stopped or position >= len(chain):
            return

        result = chain[position](context, lambda: dispatch(position + 1))
        if inspect.isawaitable(result):
            await result

    await dispatch(0)
    return not context.stopped


def resolve_policy(
    context: MiddlewareContext,
    path: str,
    explicit: Optional[T],
    default: T,
) -> T:
    """按显式配置、context.state.policy、默认值的优先级解析策略。"""

    if explicit is not None:
        return explicit

    value: Any = context.state.get("policy")
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return default if value is None else value
