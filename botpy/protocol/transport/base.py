from typing import Awaitable, Callable, Protocol, runtime_checkable

from ..models import RawEvent


EventHandler = Callable[[RawEvent], Awaitable[None]]


@runtime_checkable
class EventTransport(Protocol):
    """事件传输的最小接口，WebSocket、Webhook 和测试传输均实现它。"""

    async def start(self, handler: EventHandler) -> None:
        """开始接收事件，直到 close() 被调用。"""

    async def close(self) -> None:
        """停止接收事件并释放资源。"""
