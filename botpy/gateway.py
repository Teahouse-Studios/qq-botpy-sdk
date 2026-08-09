# -*- coding: utf-8 -*-
import asyncio
import json
import traceback
from typing import Optional

from aiohttp import WSMessage, ClientWebSocketResponse, TCPConnector, ClientSession, WSMsgType
from ssl import SSLContext

from . import logging
from .connection import ConnectionSession
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
        self._INVALID_RECONNECT_CODE = [9001, 9005]
        self._AUTH_FAIL_CODE = [4004]

    async def on_error(self, exception: BaseException):
        _log.error("[botpy] websocket连接: %s, 异常信息 : %s" % (self._conn, exception))
        traceback.print_exc()
        self._stop_heartbeat()
        self._queue_reconnect()

    async def on_closed(self, close_status_code, close_msg):
        _log.info("[botpy] 关闭, 返回码: %s" % close_status_code + ", 返回信息: %s" % close_msg)
        self._stop_heartbeat()
        if close_status_code in self._AUTH_FAIL_CODE:
            _log.info("[botpy] 鉴权失败，重置token...")
            self._session["token"].access_token = None
        # 这种不能重新链接
        if close_status_code in self._INVALID_RECONNECT_CODE or not self._can_reconnect:
            _log.info("[botpy] 无法重连，创建新连接!")
            self._session["session_id"] = ""
            self._session["last_seq"] = None
        # 断连后启动一个新的链接并透传当前的session，不使用内部重连的方式，避免死循环
        self._queue_reconnect()

    def _queue_reconnect(self):
        """确保同一个 websocket 实例只被放回重连队列一次。"""
        if self._reconnect_queued:
            return
        self._reconnect_queued = True
        self._connection.add(self._session)

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

    async def on_message(self, ws, message):
        _log.debug("[botpy] 接收消息: %s" % message)
        msg = json.loads(message)

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

        if event and opcode == self.WS_DISPATCH_EVENT:
            event = msg["t"].lower()
            try:
                func = self._parser[event]
            except KeyError:
                _log.error("_parser unknown event %s.", event)
            else:
                func(msg)

        # 在网关事件完成解析和分发后再记录序列号，避免 Resume 跳过处理失败的事件。
        if isinstance(event_seq, int) and event_seq >= 0:
            self._session["last_seq"] = event_seq

    async def on_connected(self, ws: ClientWebSocketResponse):
        self._conn = ws
        if self._conn is None:
            raise Exception("[botpy] websocket连接失败")
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

        # adding SSLContext-containing connector to prevent SSL certificate verify failed error
        async with ClientSession(connector=TCPConnector(limit=10, ssl=SSLContext())) as session:
            async with session.ws_connect(self._session["url"]) as ws_conn:
                while True:
                    msg: WSMessage
                    msg = await ws_conn.receive()
                    if msg.type == WSMsgType.TEXT:
                        await self.on_message(ws_conn, msg.data)
                    elif msg.type == WSMsgType.ERROR:
                        await self.on_error(ws_conn.exception())
                        await ws_conn.close()
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
        self._session["shards"]["shard_id"] = data["shard"][0]
        self._session["shards"]["shard_count"] = data["shard"][1]
        self.user = data["user"]
        return data

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
            _log.warning("[botpy] Session 已失效，准备重新鉴权...")
            await self._close_for_reconnect("invalid session", can_resume=False)
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
