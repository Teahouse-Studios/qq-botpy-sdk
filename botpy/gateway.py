# -*- coding: utf-8 -*-
import asyncio
import json
from typing import Optional

from aiohttp import WSMessage, ClientWebSocketResponse, TCPConnector, ClientSession, WSMsgType

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
        self._conn: Optional[ClientWebSocketResponse] = None
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
        if self._conn is None or self._conn.closed:
            await self._queue_reconnect()

    async def on_closed(self, close_status_code, close_msg):
        _log.info("[botpy] 关闭, 返回码: %s" % close_status_code + ", 返回信息: %s" % close_msg)
        self._stop_heartbeat()
        if self._closing:
            return
        if self._reconnect_queued:
            return

        action = self._reconnect_policy.handle_close(close_status_code)
        if not self._can_reconnect:
            action = CloseAction(
                should_reconnect=True,
                clear_session=True,
                refresh_token=True,
                reason="resume is not allowed",
            )

        if action.refresh_token:
            _log.info("[botpy] Gateway 要求刷新 token: %s", action.reason)
            self._session["token"].access_token = None
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
        if self._conn is not None and not self._conn.closed:
            await self._conn.close(code=4000, message=reason.encode("utf-8")[:123])
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
        self._stop_heartbeat()
        reconnect_wait_task = self._reconnect_wait_task
        if reconnect_wait_task and reconnect_wait_task is not asyncio.current_task():
            reconnect_wait_task.cancel()
        if self._conn is not None and not self._conn.closed:
            await self._conn.close(code=1000, message=b"client closing")

    async def on_message(self, ws, message):
        _log.debug("[botpy] 接收消息: %s" % message)
        msg = json.loads(message)
        if not isinstance(msg, dict):
            raise ValueError("gateway payload must be an object")

        if await self._is_system_event(msg, ws):
            return

        event = msg.get("t")
        opcode = msg.get("op")
        event_seq = msg.get("s")

        if event == "READY":
            ready = await self._ready_handler(msg)
            self._start_heartbeat()
            _log.info(f"[botpy] 机器人「{ready['user']['username']}」启动成功！")

        if event == "RESUMED":
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
            self._session["last_seq"] = event_seq
            await self._persist_session()

    async def on_connected(self, ws: ClientWebSocketResponse):
        self._conn = ws
        if self._conn is None:
            raise Exception("[botpy] websocket连接失败")
        self._reconnect_policy.on_connected()
        if self._session["session_id"]:
            await self.ws_resume()
        else:
            await self.ws_identify()

    async def ws_connect(self):
        """
        websocket向服务器端发起链接，并定时发送心跳
        """

        _log.info("[botpy] 启动中...")
        ws_url = self._session["url"]
        if not ws_url:
            raise Exception("[botpy] 会话url为空")

        async with ClientSession(connector=TCPConnector(limit=10)) as session:
            async with session.ws_connect(self._session["url"]) as ws_conn:
                while True:
                    msg: WSMessage
                    msg = await ws_conn.receive()
                    if msg.type == WSMsgType.TEXT:
                        await self.on_message(ws_conn, msg.data)
                    elif msg.type == WSMsgType.ERROR:
                        exception = ws_conn.exception() or RuntimeError("websocket transport error")
                        await self.on_error(exception)
                        await ws_conn.close()
                        await self.on_closed(ws_conn.close_code, str(exception))
                    elif msg.type == WSMsgType.CLOSED or msg.type == WSMsgType.CLOSE:
                        await self.on_closed(ws_conn.close_code, msg.extra)
                    if ws_conn.closed:
                        _log.info("[botpy] ws关闭, 停止接收消息!")
                        break

    async def ws_identify(self):
        """websocket鉴权"""
        if not self._session["intent"]:
            self._session["intent"] = 1

        _log.info("[botpy] 鉴权中...")
        await self._session["token"].check_token()
        payload = {
            "op": self.WS_IDENTITY,
            "d": {
                "shard": [
                    self._session["shards"]["shard_id"],
                    self._session["shards"]["shard_count"],
                ],
                "token": self._session["token"].get_string(),
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
        if isinstance(self._conn, ClientWebSocketResponse):
            if self._conn.closed:
                _log.debug("[botpy] ws连接已关闭! ws对象: %s" % self._conn)
            else:
                await self._conn.send_str(data=send_msg)

    async def ws_resume(self):
        """
        websocket重连
        """
        _log.info("[botpy] 重连启动...")
        await self._session["token"].check_token()
        payload = {
            "op": self.WS_RESUME,
            "d": {
                "token": self._session["token"].get_string(),
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
        self._heartbeat_task = self._connection.loop.create_task(
            self._send_heart(self._heartbeat_interval)
        )

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
                if self._conn.closed:
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
            if self._conn is not None and not self._conn.closed:
                await self._conn.close(code=4000, message=b"heartbeat failure")
