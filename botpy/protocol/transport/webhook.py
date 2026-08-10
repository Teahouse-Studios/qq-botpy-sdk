import asyncio
from dataclasses import dataclass, field
import inspect
import json
import logging
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Protocol, Union, runtime_checkable

from aiohttp import web

from ..events import parse_gateway_event
from .base import EventHandler
from .webhook_verify import sign_validation_response, verify_webhook_signature


OP_DISPATCH = 0
OP_HTTP_CALLBACK_ACK = 12
OP_VALIDATION = 13

HeaderValue = Union[str, list[str], tuple[str, ...]]


@dataclass(frozen=True)
class WebhookRequest:
    body: bytes
    headers: Mapping[str, HeaderValue] = field(default_factory=dict)


@dataclass(frozen=True)
class WebhookResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


WebhookRequestHandler = Callable[[WebhookRequest], Awaitable[WebhookResponse]]
StartedHandler = Callable[[Mapping[str, Any]], Union[Awaitable[None], None]]
ErrorHandler = Callable[[BaseException], Union[Awaitable[None], None]]


@runtime_checkable
class WebhookServerAdapter(Protocol):
    async def listen(
        self,
        host: str,
        port: int,
        path: str,
        handler: WebhookRequestHandler,
    ) -> None: ...

    async def close(self) -> None: ...


class AiohttpWebhookServer:
    """基于项目现有 aiohttp 依赖的默认 Webhook HTTP 服务器。"""

    def __init__(self, *, max_body_size: int = 1024 * 1024) -> None:
        self.max_body_size = max(1, max_body_size)
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self.bound_port: Optional[int] = None

    async def listen(
        self,
        host: str,
        port: int,
        path: str,
        handler: WebhookRequestHandler,
    ) -> None:
        if self._runner is not None:
            raise RuntimeError("webhook server is already running")

        application = web.Application(client_max_size=self.max_body_size)

        async def handle_aiohttp_request(request: web.Request) -> web.Response:
            response = await handler(
                WebhookRequest(
                    body=await request.read(),
                    headers={key.lower(): value for key, value in request.headers.items()},
                )
            )
            headers = {"Content-Type": "application/json", **dict(response.headers)}
            return web.Response(status=response.status, body=response.body, headers=headers)

        application.router.add_post(path, handle_aiohttp_request)
        self._runner = web.AppRunner(application)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=host, port=port)
        try:
            await self._site.start()
        except Exception:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            raise

        server = getattr(self._site, "_server", None)
        sockets = getattr(server, "sockets", None)
        if sockets:
            self.bound_port = sockets[0].getsockname()[1]
        else:
            self.bound_port = port

    async def close(self) -> None:
        runner = self._runner
        self._runner = None
        self._site = None
        self.bound_port = None
        if runner is not None:
            await runner.cleanup()


class WebhookTransport:
    """QQ Webhook 事件传输，实现验签、URL 验证和 op:12 ACK。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        host: str = "0.0.0.0",
        port: int = 8080,
        path: str = "/",
        server: Optional[WebhookServerAdapter] = None,
        logger: Optional[logging.Logger] = None,
        on_started: Optional[StartedHandler] = None,
        on_error: Optional[ErrorHandler] = None,
    ) -> None:
        if not app_id:
            raise ValueError("app_id is required")
        if not app_secret:
            raise ValueError("app_secret is required")
        if not path.startswith("/"):
            raise ValueError("webhook path must start with '/'")

        self.app_id = app_id
        self.app_secret = app_secret
        self.host = host
        self.port = port
        self.path = path
        self.server = server or AiohttpWebhookServer()
        self._logger = logger or logging.getLogger("botpy.protocol.webhook")
        self._on_started = on_started
        self._on_error = on_error
        self._handler: Optional[EventHandler] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._dispatch_tasks: set[asyncio.Task] = set()
        self._server_started = False
        self._running = False

    async def start(self, handler: EventHandler) -> None:
        if self._running:
            raise RuntimeError("webhook transport is already running")
        self._running = True
        self._handler = handler
        self._stop_event = asyncio.Event()
        try:
            await self.server.listen(self.host, self.port, self.path, self.handle_request)
            self._server_started = True
            if not self._stop_event.is_set():
                await self._invoke_optional(
                    self._on_started,
                    {
                        "transport": "webhook",
                        "host": self.host,
                        "port": getattr(self.server, "bound_port", None) or self.port,
                        "path": self.path,
                    },
                )
            await self._stop_event.wait()
        finally:
            try:
                await self._shutdown()
            finally:
                self._handler = None
                self._running = False

    async def close(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        await self._shutdown()

    async def _shutdown(self) -> None:
        server_error: Optional[BaseException] = None
        if self._server_started:
            # 先清除标记，避免 close() 与 start() 的 finally 重复关闭自定义服务器。
            self._server_started = False
            try:
                await self.server.close()
            except BaseException as exc:
                server_error = exc

        tasks = tuple(self._dispatch_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._dispatch_tasks.clear()

        if server_error is not None:
            raise server_error

    async def handle_request(self, request: WebhookRequest) -> WebhookResponse:
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json_response(400, {"error": "invalid json"})
        if not isinstance(payload, dict) or not isinstance(payload.get("op"), int):
            return self._json_response(400, {"error": "invalid payload"})

        if payload["op"] == OP_VALIDATION:
            return self._handle_validation(payload)

        timestamp = self._get_header(request.headers, "x-signature-timestamp")
        signature = self._get_header(request.headers, "x-signature-ed25519")
        if not timestamp or not signature:
            return self._json_response(401, {"error": "missing signature"})
        if not verify_webhook_signature(
            body=request.body,
            timestamp=timestamp,
            signature=signature,
            bot_secret=self.app_secret,
        ):
            return self._json_response(401, {"error": "invalid signature"})

        if payload["op"] == OP_DISPATCH:
            task = asyncio.create_task(self._dispatch(payload))
            self._dispatch_tasks.add(task)
            task.add_done_callback(self._dispatch_tasks.discard)

        return self._json_response(200, {"op": OP_HTTP_CALLBACK_ACK, "d": 0})

    def _handle_validation(self, payload: Mapping[str, Any]) -> WebhookResponse:
        data = payload.get("d")
        if not isinstance(data, Mapping):
            return self._json_response(400, {"error": "invalid validation"})
        plain_token = data.get("plain_token")
        event_ts = data.get("event_ts")
        if not isinstance(plain_token, str) or not plain_token or not isinstance(event_ts, str) or not event_ts:
            return self._json_response(400, {"error": "invalid validation"})
        response = sign_validation_response(
            plain_token=plain_token,
            event_ts=event_ts,
            bot_secret=self.app_secret,
        )
        return self._json_response(200, response)

    async def _dispatch(self, payload: Mapping[str, Any]) -> None:
        handler = self._handler
        if handler is None:
            return
        try:
            await handler(parse_gateway_event(payload))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.error("[botpy] Webhook 事件处理失败: %s", exc)
            await self._invoke_optional(self._on_error, exc)

    @staticmethod
    async def _invoke_optional(callback, *args: Any) -> None:
        if callback is None:
            return
        result = callback(*args)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _get_header(headers: Mapping[str, HeaderValue], name: str) -> Optional[str]:
        for key, value in headers.items():
            if key.lower() != name:
                continue
            if isinstance(value, str):
                return value
            if isinstance(value, (list, tuple)) and value:
                return value[0]
        return None

    @staticmethod
    def _json_response(status: int, payload: Mapping[str, Any]) -> WebhookResponse:
        return WebhookResponse(
            status=status,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
