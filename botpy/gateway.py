# -*- coding: utf-8 -*-
import asyncio
import inspect
import json
from typing import Any, Optional

import httpx
from httpx_ws import WebSocketDisconnect, aconnect_ws

from . import logging
from .connection import ConnectionSession
from .protocol.events import parse_gateway_event
from .protocol.reconnect import CloseAction, ReconnectPolicy
from .protocol.models import SessionState
from .protocol.session import SessionStore
from .protocol.transport import EventHandler
from .types import gateway
from .types.session import Session

_log = logging.get_logger()


class BotWebSocket:
    """Bot的Websocket实现

    CODE	名称	客户端操作	描述
    0	Dispatch	Receive	服务端进行消息推送
    1	Heartbeat	Send/Receive	客户端或服务端发送心跳
    2	Identify	Send	客户端发送鉴权
    6	Resume	Send	客户端恢复连接
    7	Reconnect	Receive	服务端通知客户端重新连接
    9	Invalid Session	Receive	当identify或resume的时候，如果参数有错，服务端会返回该消息
    10	Hello	Receive	当客户端与网关建立ws连接之后，网关下发的第一条消息
    11	Heartbeat ACK	Receive	当发送心跳成功之后，就会收到该消息
    """

    WS_DISPATCH_EVENT = 0
    WS_HEARTBEAT = 1
    WS_IDENTITY = 2
    WS_RESUME = 6
    WS_RECONNECT = 7
    WS_INVALID_SESSION = 9
    WS_HELLO = 10
    WS_HEARTBEAT_ACK = 11
    DEFAULT_HEARTBEAT_INTERVAL = 45.0

    def __init__(self, session: Session, _connection: ConnectionSession):
        # ``httpx-ws`` deliberately keeps its WebSocket object independent of
        # httpx's HTTP response types.  Keep the connection typed as ``Any``
        # here so callers can inject lightweight test doubles as before.
        self._conn: Optional[Any] = None
        self._session = session
        self._connection = _connection
        self._parser = _connection.parser
        self._can_reconnect = True
        self._reconnect_queued = False
        self._heartbeat_interval = self.DEFAULT_HEARTBEAT_INTERVAL
        self._heartbeat_acknowledged = True
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._event_handler: Optional[EventHandler] = None
        self._closing = False
        self._sleep = asyncio.sleep
        self._reconnect_wait_task: Optional[asyncio.Task] = None
        self._gateway_access_token: Optional[str] = None
        claim_gateway = getattr(self._connection, "claim_gateway", None)
        if claim_gateway is not None:
            claim_gateway(self._session, self)
        reconnect_policy = self._session.get("reconnect_policy")
        if not isinstance(reconnect_policy, ReconnectPolicy):
            reconnect_policy = ReconnectPolicy()
            self._session["reconnect_policy"] = reconnect_policy
        self._reconnect_policy = reconnect_policy

    async def on_error(self, exception: BaseException):
        if self._closing:
            return
        _log.error("[botpy] websocket连接: %s, 异常信息 : %s" % (self._conn, exception))
        self._stop_heartbeat()
        if self._conn is None or self._ws_is_closed(self._conn):
            if self._mark_disconnected():
                await self._queue_reconnect()

    async def on_closed(self, close_status_code, close_msg):
        if not self._mark_disconnected():
            self._stop_heartbeat()
            _log.debug("[botpy] 忽略已被替换的 Gateway 连接关闭回调")
            return
        _log.info("[botpy] 关闭, 返回码: %s" % close_status_code + ", 返回信息: %s" % close_msg)
        # The task which detected a missed heartbeat may already own the
        # delayed reconnect.  A duplicate CLOSED frame must not cancel that
        # task before it can put the session back into the connection queue.
        if self._reconnect_queued:
            return
        self._stop_heartbeat()
        if self._closing:
            return

        action = self._reconnect_policy.handle_close(close_status_code)
        if not self._can_reconnect:
            action = CloseAction(
                should_reconnect=True,
                clear_session=True,
                reason="resume is not allowed",
            )

        if action.refresh_token:
            _log.info("[botpy] Gateway 要求刷新 token: %s", action.reason)
            self._clear_rejected_access_token()
        if action.clear_session:
            _log.info("[botpy] Gateway Session 已清理: %s", action.reason)
            self._session["session_id"] = ""
            self._session["last_seq"] = None
            await self._clear_persisted_session()
        if action.fatal:
            _log.error("[botpy] Gateway 致命关闭，不再重连: %s", action.reason)
            return
        if action.should_reconnect:
            await self._queue_reconnect(action.reconnect_delay)

    async def _queue_reconnect(self, custom_delay: Optional[float] = None):
        """确保同一个 websocket 实例只被放回重连队列一次。"""
        if self._closing or self._reconnect_queued:
            return
        self._reconnect_queued = True
        delay = self._reconnect_policy.next_delay(custom_delay)
        if delay is None:
            _log.error("[botpy] Gateway 重连次数已耗尽")
            return
        _log.info("[botpy] %.1f 秒后进行第 %s 次 Gateway 重连", delay, self._reconnect_policy.attempts)
        if delay > 0:
            self._reconnect_wait_task = asyncio.create_task(self._sleep(delay))
            try:
                await self._reconnect_wait_task
            except asyncio.CancelledError:
                return
            finally:
                self._reconnect_wait_task = None
        if not self._closing:
            self._connection.add(self._session, is_reconnect=True)

    def _stop_heartbeat(self):
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _close_for_reconnect(self, reason: str, can_resume: bool):
        """主动关闭连接，并根据服务端指令决定 Resume 或重新 Identify。"""
        self._can_reconnect = can_resume
        self._mark_disconnected()
        if self._conn is not None and not self._ws_is_closed(self._conn):
            await self._close_ws(self._conn, 4000, reason)
        await self.on_closed(4000, reason)

    async def start(self, handler: EventHandler) -> None:
        """作为 EventTransport 启动，同时保留旧事件解析分发。"""
        self._event_handler = handler
        self._closing = False
        await self.ws_connect()

    async def close(self) -> None:
        """停止当前传输且不进入重连队列。"""
        self._closing = True
        self._can_reconnect = False
        self._mark_disconnected()
        self._stop_heartbeat()
        reconnect_wait_task = self._reconnect_wait_task
        if reconnect_wait_task and reconnect_wait_task is not asyncio.current_task():
            reconnect_wait_task.cancel()
        if self._conn is not None and not self._ws_is_closed(self._conn):
            await self._close_ws(self._conn, 1000, "client closing")

    async def on_message(self, ws, message):
        _log.debug("[botpy] 接收消息: %s" % message)
        msg = json.loads(message)
        if not isinstance(msg, dict):
            raise ValueError("gateway payload must be an object")
        if not self._is_current_gateway():
            _log.debug("[botpy] 忽略已被替换的 Gateway 连接的迟到消息")
            return

        if await self._is_system_event(msg, ws):
            return

        event = msg.get("t")
        opcode = msg.get("op")
        event_seq = msg.get("s")

        if event == "READY":
            ready = await self._ready_handler(msg)
            if not self._mark_ready():
                _log.debug("[botpy] 忽略已被替换的 Gateway READY 事件")
                return
            self._reconnect_policy.on_connected()
            self._start_heartbeat()
            _log.info(f"[botpy] 机器人「{ready['user']['username']}」启动成功！")

        if event == "RESUMED":
            if not self._mark_ready():
                _log.debug("[botpy] 忽略已被替换的 Gateway RESUMED 事件")
                return
            self._reconnect_policy.on_connected()
            self._start_heartbeat()
            _log.info("[botpy] 机器人重连成功! ")

        if opcode == self.WS_DISPATCH_EVENT:
            if event:
                parser_name = event.lower()
                try:
                    func = self._parser[parser_name]
                except KeyError:
                    if self._event_handler is None:
                        _log.warning("[botpy] 未识别 Gateway 事件: %s", parser_name)
                    else:
                        _log.debug("[botpy] Gateway 事件由 raw handler 接管: %s", parser_name)
                else:
                    func(msg)
            else:
                _log.warning("[botpy] Gateway Dispatch 缺少事件类型 t")

            if self._event_handler is not None:
                await self._event_handler(parse_gateway_event(msg))

        # 在网关事件完成解析和分发后再记录序列号，避免 Resume 跳过处理失败的事件。
        if isinstance(event_seq, int) and not isinstance(event_seq, bool) and event_seq >= 0:
            # Event handlers are awaitable.  A replacement connection may
            # have claimed the shard while one was running, in which case the
            # old event must not overwrite the replacement's resume cursor.
            if not self._is_current_gateway():
                _log.debug("[botpy] 忽略已被替换的 Gateway 连接的迟到序列号")
                return
            self._session["last_seq"] = event_seq
            await self._persist_session()

    async def on_connected(self, ws: Any):
        self._conn = ws
        if self._conn is None:
            raise Exception("[botpy] websocket连接失败")
        if not self._is_current_gateway():
            _log.debug("[botpy] 忽略已被替换的 Gateway 连接鉴权")
            return
        if self._session["session_id"]:
            await self.ws_resume()
        else:
            await self.ws_identify()

    def _mark_ready(self) -> bool:
        marker = getattr(self._connection, "mark_ready", None)
        if marker is not None:
            return marker(self._session, owner=self) is not False
        return True

    def _is_current_gateway(self) -> bool:
        checker = getattr(self._connection, "is_gateway_owner", None)
        if checker is not None:
            return checker(self._session, self) is not False
        return True

    def _mark_disconnected(self) -> bool:
        marker = getattr(self._connection, "mark_disconnected", None)
        if marker is not None:
            return marker(self._session, owner=self) is not False
        return True

    def _clear_rejected_access_token(self) -> None:
        token = self._session["token"]
        rejected_token = self._gateway_access_token
        compare_and_clear = getattr(token, "clear_access_token", None)
        if rejected_token is not None and callable(compare_and_clear):
            if not compare_and_clear(rejected_token):
                _log.info("[botpy] Gateway 拒绝的是旧 token，保留当前 access token")
            return
        token.access_token = None

    async def _get_gateway_token(self) -> str:
        token = self._session["token"]
        get_access_token = getattr(token, "get_access_token", None)
        if callable(get_access_token):
            token_value = await get_access_token()
        else:
            await token.check_token()
            token_value = token.access_token
        self._gateway_access_token = token_value

        get_string = token.get_string
        try:
            inspect.signature(get_string).bind(token_value)
        except (TypeError, ValueError):
            return get_string()
        return get_string(token_value)

    async def ws_connect(self):
        """
        websocket向服务器端发起链接，并定时发送心跳
        """

        _log.info("[botpy] 启动中...")
        ws_url = self._session["url"]
        if not ws_url:
            raise Exception("[botpy] 会话url为空")

        # httpx does not provide a WebSocket client itself; httpx-ws bridges
        # an ``httpx.AsyncClient`` to a standards-compliant WebSocket stream.
        # Keep a short-lived HTTP client per Gateway connection and bound
        # concurrent connection attempts.
        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=10),
            timeout=None,
            verify=self._session.get("ssl") if self._session.get("ssl") is not None else True,
        ) as session:
            async with aconnect_ws(self._session["url"], session) as ws_conn:
                self._conn = ws_conn
                if self._closing:
                    await self._close_ws(ws_conn, 1000, "client closing")
                    return
                while True:
                    try:
                        msg = await ws_conn.receive()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exception:
                        # A network failure can surface as an exception rather
                        # than a close frame with httpx-ws.  Route it through
                        # the existing reconnect policy used for receive failures.
                        if isinstance(exception, WebSocketDisconnect):
                            await self.on_closed(
                                getattr(exception, "code", None),
                                getattr(exception, "reason", None),
                            )
                            break
                        await self.on_error(exception)
                        if not self._ws_is_closed(ws_conn):
                            # 1006 is a receive-only synthetic status and
                            # must not be sent in a close frame.
                            await self._close_ws(ws_conn, 1011, str(exception))
                        await self.on_closed(
                            getattr(ws_conn, "close_code", None) or getattr(exception, "code", None),
                            getattr(exception, "reason", None) or str(exception),
                        )
                        break

                    if isinstance(msg, str):
                        await self.on_message(ws_conn, msg)
                    elif isinstance(msg, (bytes, bytearray, memoryview)):
                        # Gateway payloads are JSON text.  Decode binary frames
                        # defensively to preserve the old behaviour for servers
                        # that mark a text payload as binary.
                        await self.on_message(ws_conn, bytes(msg).decode("utf-8"))
                    else:
                        msg_type = getattr(msg, "type", None)
                        type_name = getattr(msg_type, "name", msg_type)
                        # httpx-ws 0.9 yields wsproto event objects whose
                        # ``type`` attribute is absent; use the class name as
                        # the stable discriminator for those events.
                        type_name = str(type_name or msg.__class__.__name__).casefold()
                        data = getattr(msg, "data", None)
                        if "text" in type_name and isinstance(data, str):
                            await self.on_message(ws_conn, data)
                        elif ("bytes" in type_name or "binary" in type_name) and data is not None:
                            await self.on_message(ws_conn, bytes(data).decode("utf-8"))
                        elif "error" in type_name:
                            exception = getattr(ws_conn, "exception", None)
                            if callable(exception):
                                exception = exception() or RuntimeError("websocket transport error")
                            exception = exception or RuntimeError("websocket transport error")
                            await self.on_error(exception)
                            await self.on_closed(
                                getattr(ws_conn, "close_code", None),
                                str(exception),
                            )
                        elif "close" in type_name:
                            await self.on_closed(
                                getattr(ws_conn, "close_code", None) or getattr(msg, "code", None),
                                getattr(msg, "reason", None) or getattr(msg, "extra", None),
                            )
                    if self._ws_is_closed(ws_conn):
                        _log.info("[botpy] ws关闭, 停止接收消息!")
                        break

    async def ws_identify(self):
        """websocket鉴权"""
        if not self._session["intent"]:
            self._session["intent"] = 1

        _log.info("[botpy] 鉴权中...")
        gateway_token = await self._get_gateway_token()
        if not self._is_current_gateway():
            _log.debug("[botpy] 忽略已被替换的 Gateway Identify")
            return
        payload = {
            "op": self.WS_IDENTITY,
            "d": {
                "shard": [
                    self._session["shards"]["shard_id"],
                    self._session["shards"]["shard_count"],
                ],
                "token": gateway_token,
                "intents": self._session["intent"],
            },
        }

        await self.send_msg(json.dumps(payload))

    async def send_msg(self, event_json):
        """
        websocket发送消息
        :param event_json:
        """
        send_msg = event_json
        _log.debug("[botpy] 发送消息: %s" % send_msg)
        if self._conn is None:
            return
        if self._ws_is_closed(self._conn):
            _log.debug("[botpy] ws连接已关闭! ws对象: %s" % self._conn)
            return
        send_text = getattr(self._conn, "send_text", None)
        if callable(send_text):
            await send_text(send_msg)
            return
        # Keep compatibility with legacy test doubles and custom
        # transports that expose ``send_str``.
        send_str = getattr(self._conn, "send_str", None)
        if callable(send_str):
            await send_str(data=send_msg)

    @staticmethod
    async def _close_ws(ws: Any, code: int, reason: str = "") -> None:
        """Close an httpx-ws socket or a legacy test double."""

        close = getattr(ws, "close", None)
        if not callable(close):
            return
        try:
            await close(code=code, reason=reason[:123])
        except TypeError:
            # Some legacy doubles use ``message`` bytes instead of ``reason``.
            await close(code=code, message=reason.encode("utf-8")[:123])

    @staticmethod
    def _ws_is_closed(ws: Any) -> bool:
        """Return a closed state for httpx-ws and legacy socket doubles."""

        if bool(getattr(ws, "closed", False)):
            return True
        state = getattr(getattr(ws, "connection", None), "state", None)
        state_name = str(state).casefold() if state is not None else ""
        return "closed" in state_name or "closing" in state_name

    async def ws_resume(self):
        """
        websocket重连
        """
        _log.info("[botpy] 重连启动...")
        gateway_token = await self._get_gateway_token()
        if not self._is_current_gateway():
            _log.debug("[botpy] 忽略已被替换的 Gateway Resume")
            return
        payload = {
            "op": self.WS_RESUME,
            "d": {
                "token": gateway_token,
                "session_id": self._session["session_id"],
                "seq": self._session["last_seq"],
            },
        }

        await self.send_msg(json.dumps(payload))

    async def _ready_handler(self, message_event) -> gateway.ReadyEvent:
        data = message_event["d"]
        self.version = data["version"]
        self._session["session_id"] = data["session_id"]
        shard = data.get("shard")
        if isinstance(shard, (list, tuple)) and len(shard) == 2:
            if isinstance(shard[0], int) and shard[0] >= 0:
                self._session["shards"]["shard_id"] = shard[0]
            if isinstance(shard[1], int) and shard[1] > 0:
                self._session["shards"]["shard_count"] = shard[1]
        self.user = data["user"]
        return data

    async def _persist_session(self) -> None:
        store = self._session.get("session_store")
        session_id = self._session.get("session_id")
        sequence = self._session.get("last_seq")
        if not isinstance(store, SessionStore) or not session_id or sequence is None:
            return
        shard = self._session["shards"]
        try:
            await store.save(
                self._session["token"].app_id,
                SessionState(
                    session_id=session_id,
                    sequence=sequence,
                    shard_id=shard["shard_id"],
                    shard_count=shard["shard_count"],
                ),
            )
        except Exception as exc:
            _log.warning("[botpy] 保存 Gateway Session 失败: %s", exc)

    async def _clear_persisted_session(self) -> None:
        store = self._session.get("session_store")
        if not isinstance(store, SessionStore):
            return
        try:
            await store.clear(
                self._session["token"].app_id,
                self._session["shards"]["shard_id"],
            )
        except Exception as exc:
            _log.warning("[botpy] 清理 Gateway Session 失败: %s", exc)

    async def _is_system_event(self, message_event, ws):
        """
        系统事件
        :param message_event:消息
        :param ws:websocket
        :return:
        """
        event_op = message_event["op"]
        if event_op == self.WS_HELLO:
            heartbeat_interval = message_event.get("d", {}).get("heartbeat_interval")
            if isinstance(heartbeat_interval, (int, float)) and heartbeat_interval > 0:
                self._heartbeat_interval = heartbeat_interval / 1000
            else:
                _log.warning(
                    "[botpy] Hello 消息缺少有效 heartbeat_interval，使用默认值 %s 秒",
                    self.DEFAULT_HEARTBEAT_INTERVAL,
                )
            await self.on_connected(ws)
            return True
        if event_op == self.WS_HEARTBEAT:
            # 服务端也可能主动要求客户端立即发送一次心跳。
            await self._send_heartbeat(track_ack=False)
            return True
        if event_op == self.WS_HEARTBEAT_ACK:
            self._heartbeat_acknowledged = True
            return True
        if event_op == self.WS_RECONNECT:
            _log.info("[botpy] 收到服务端重连指令，准备 Resume...")
            await self._close_for_reconnect("server requested reconnect", can_resume=True)
            return True
        if event_op == self.WS_INVALID_SESSION:
            can_resume = message_event.get("d") is True
            if can_resume:
                _log.warning("[botpy] Session 暂时无效，准备 Resume...")
            else:
                _log.warning("[botpy] Session 已失效，准备重新鉴权...")
            await self._close_for_reconnect("invalid session", can_resume=can_resume)
            return True
        return False

    def _start_heartbeat(self):
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        self._heartbeat_acknowledged = True
        self._heartbeat_task = self._connection.loop.create_task(self._send_heart(self._heartbeat_interval))

    async def _send_heartbeat(self, track_ack: bool = True):
        payload = {
            "op": self.WS_HEARTBEAT,
            "d": self._session["last_seq"],
        }
        if track_ack:
            self._heartbeat_acknowledged = False
        await self.send_msg(json.dumps(payload))

    async def _send_heart(self, interval):
        """
        心跳包
        :param interval: 间隔时间
        """
        _log.info("[botpy] 心跳维持启动...")
        try:
            while True:
                if self._conn is None:
                    _log.debug("[botpy] 连接已关闭!")
                    return
                if self._ws_is_closed(self._conn):
                    _log.debug("[botpy] ws连接已关闭, 心跳检测停止，ws对象: %s" % self._conn)
                    return

                if not self._heartbeat_acknowledged:
                    _log.warning("[botpy] 未在心跳周期内收到 ACK，准备重新连接...")
                    await self._close_for_reconnect("heartbeat ACK timeout", can_resume=True)
                    return

                await self._send_heartbeat()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        except Exception as exception:
            await self.on_error(exception)
            if self._conn is not None and not self._ws_is_closed(self._conn):
                await self._close_ws(self._conn, 4000, "heartbeat failure")
