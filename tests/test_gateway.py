import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from botpy.connection import ConnectionSession
from botpy.gateway import BotWebSocket
from botpy.protocol import RawEvent
from botpy.protocol.models import SessionState
from botpy.protocol.session import MemorySessionStore
from botpy.protocol.transport import EventTransport


class DummyToken:
    def __init__(self):
        self.app_id = "app-id"
        self.access_token = "token"

    async def check_token(self):
        return None

    def get_string(self):
        return "Bot appid.token"


class DummyConnection:
    def __init__(self, loop, parser=None):
        self.loop = loop
        self.parser = parser or {}
        self.sessions = []

    def add(self, session, *, is_reconnect=False):
        self.sessions.append(session)


class DummyWebSocket:
    def __init__(self):
        self.closed = False
        self.close_code = None

    async def close(self, code=1000, message=b""):
        self.closed = True
        self.close_code = code


def make_session():
    return {
        "session_id": "session-id",
        "last_seq": None,
        "intent": 1,
        "token": DummyToken(),
        "url": "wss://example.invalid",
        "shards": {"shard_id": 0, "shard_count": 1},
    }


class GatewayKeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = DummyConnection(asyncio.get_running_loop())
        self.session = make_session()
        self.gateway = BotWebSocket(self.session, self.connection)
        self.gateway._sleep = AsyncMock()

    async def test_hello_uses_server_heartbeat_interval(self):
        self.gateway.on_connected = AsyncMock()

        handled = await self.gateway._is_system_event(
            {"op": self.gateway.WS_HELLO, "d": {"heartbeat_interval": 45000}},
            DummyWebSocket(),
        )

        self.assertTrue(handled)
        self.assertEqual(45, self.gateway._heartbeat_interval)
        self.gateway.on_connected.assert_awaited_once()

    async def test_ready_does_not_replace_valid_shard_count_with_zero(self):
        self.session["shards"]["shard_count"] = 4

        await self.gateway._ready_handler(
            {
                "d": {
                    "version": 1,
                    "session_id": "new-session",
                    "shard": [0, 0],
                    "user": {"username": "bot"},
                }
            }
        )

        self.assertEqual(4, self.session["shards"]["shard_count"])

    async def test_heartbeat_uses_null_until_a_sequence_is_received(self):
        self.gateway.send_msg = AsyncMock()

        await self.gateway._send_heartbeat()

        payload = json.loads(self.gateway.send_msg.await_args.args[0])
        self.assertEqual({"op": self.gateway.WS_HEARTBEAT, "d": None}, payload)

    async def test_server_heartbeat_request_is_handled_without_sequence_field(self):
        self.gateway.send_msg = AsyncMock()

        await self.gateway.on_message(
            DummyWebSocket(),
            json.dumps({"op": self.gateway.WS_HEARTBEAT, "d": None}),
        )

        payload = json.loads(self.gateway.send_msg.await_args.args[0])
        self.assertEqual({"op": self.gateway.WS_HEARTBEAT, "d": None}, payload)

    async def test_ack_timeout_closes_connection_for_resume(self):
        self.gateway._conn = DummyWebSocket()
        self.gateway.send_msg = AsyncMock()
        self.gateway._close_for_reconnect = AsyncMock()

        await self.gateway._send_heart(0)

        self.gateway._close_for_reconnect.assert_awaited_once_with(
            "heartbeat ACK timeout",
            can_resume=True,
        )

    async def test_heartbeat_ack_marks_connection_healthy(self):
        self.gateway._heartbeat_acknowledged = False

        handled = await self.gateway._is_system_event(
            {"op": self.gateway.WS_HEARTBEAT_ACK},
            DummyWebSocket(),
        )

        self.assertTrue(handled)
        self.assertTrue(self.gateway._heartbeat_acknowledged)

    async def test_dispatch_sequence_is_saved_after_parser_returns(self):
        def parse_event(payload):
            self.assertIsNone(self.session["last_seq"])

        self.connection.parser["test_event"] = parse_event

        await self.gateway.on_message(
            DummyWebSocket(),
            json.dumps({"op": 0, "s": 42, "t": "TEST_EVENT", "d": {}}),
        )

        self.assertEqual(42, self.session["last_seq"])

    async def test_dispatch_sequence_is_persisted(self):
        store = MemorySessionStore()
        self.session["session_store"] = store
        self.connection.parser["test_event"] = lambda payload: None

        await self.gateway.on_message(
            DummyWebSocket(),
            json.dumps({"op": 0, "s": 45, "t": "TEST_EVENT", "d": {}}),
        )

        saved = await store.load("app-id", 0)
        self.assertEqual(
            SessionState(session_id="session-id", sequence=45, shard_id=0, shard_count=1),
            saved,
        )

    async def test_unknown_dispatch_is_forwarded_as_raw_event(self):
        events = []

        async def handle_event(event):
            self.assertIsNone(self.session["last_seq"])
            events.append(event)

        self.gateway._event_handler = handle_event

        await self.gateway.on_message(
            DummyWebSocket(),
            json.dumps({"op": 0, "s": 43, "t": "FUTURE_EVENT", "d": {"value": 1}}),
        )

        self.assertEqual(1, len(events))
        self.assertIsInstance(events[0], RawEvent)
        self.assertEqual("FUTURE_EVENT", events[0].event_type)
        self.assertEqual(43, self.session["last_seq"])

    async def test_dispatch_without_type_is_still_forwarded(self):
        events = []

        async def handle_event(event):
            events.append(event)

        self.gateway._event_handler = handle_event

        await self.gateway.on_message(
            DummyWebSocket(),
            json.dumps({"op": 0, "s": 44, "d": {"value": 1}}),
        )

        self.assertEqual("", events[0].event_type)
        self.assertEqual(44, self.session["last_seq"])

    async def test_gateway_implements_event_transport(self):
        handler = AsyncMock()
        self.gateway.ws_connect = AsyncMock()

        await self.gateway.start(handler)

        self.assertIsInstance(self.gateway, EventTransport)
        self.gateway.ws_connect.assert_awaited_once()

    async def test_close_does_not_queue_reconnect(self):
        self.gateway._conn = DummyWebSocket()

        await self.gateway.close()
        await self.gateway.on_closed(1000, "client closing")

        self.assertTrue(self.gateway._conn.closed)
        self.assertEqual([], self.connection.sessions)

    async def test_resume_contains_latest_sequence(self):
        self.session["last_seq"] = 42
        self.gateway.send_msg = AsyncMock()

        await self.gateway.ws_resume()

        payload = json.loads(self.gateway.send_msg.await_args.args[0])
        self.assertEqual(self.gateway.WS_RESUME, payload["op"])
        self.assertEqual("session-id", payload["d"]["session_id"])
        self.assertEqual(42, payload["d"]["seq"])

    async def test_reconnect_opcode_preserves_session_and_queues_once(self):
        self.gateway._conn = DummyWebSocket()

        await self.gateway._is_system_event(
            {"op": self.gateway.WS_RECONNECT},
            self.gateway._conn,
        )
        await self.gateway.on_closed(4000, "duplicate close notification")

        self.assertEqual("session-id", self.session["session_id"])
        self.assertEqual(1, len(self.connection.sessions))

    async def test_invalid_session_clears_resume_state(self):
        self.session["last_seq"] = 42
        self.gateway._conn = DummyWebSocket()

        await self.gateway._is_system_event(
            {"op": self.gateway.WS_INVALID_SESSION},
            self.gateway._conn,
        )

        self.assertEqual("", self.session["session_id"])
        self.assertIsNone(self.session["last_seq"])
        self.assertEqual(1, len(self.connection.sessions))

    async def test_resumable_invalid_session_preserves_resume_state(self):
        self.session["last_seq"] = 42
        self.gateway._conn = DummyWebSocket()

        await self.gateway._is_system_event(
            {"op": self.gateway.WS_INVALID_SESSION, "d": True},
            self.gateway._conn,
        )

        self.assertEqual("session-id", self.session["session_id"])
        self.assertEqual(42, self.session["last_seq"])
        self.assertEqual(1, len(self.connection.sessions))

    async def test_rate_limit_close_uses_cooldown(self):
        await self.gateway.on_closed(4008, "rate limited")

        self.gateway._sleep.assert_awaited_once_with(60.0)
        self.assertEqual(1, len(self.connection.sessions))

    async def test_fatal_intents_close_does_not_reconnect(self):
        await self.gateway.on_closed(4915, "disallowed intents")

        self.gateway._sleep.assert_not_awaited()
        self.assertEqual([], self.connection.sessions)

    async def test_invalid_sequence_close_clears_session_and_token(self):
        self.session["last_seq"] = 42
        store = MemorySessionStore()
        self.session["session_store"] = store
        await store.save(
            "app-id",
            SessionState(session_id="session-id", sequence=42, shard_id=0, shard_count=1),
        )

        await self.gateway.on_closed(4007, "invalid sequence")

        self.assertEqual("", self.session["session_id"])
        self.assertIsNone(self.session["last_seq"])
        self.assertIsNone(self.session["token"].access_token)
        self.assertEqual(1, len(self.connection.sessions))
        self.assertIsNone(await store.load("app-id", 0))

    async def test_close_cancels_pending_reconnect_wait(self):
        wait_started = asyncio.Event()

        async def wait_forever(delay):
            wait_started.set()
            await asyncio.Event().wait()

        self.gateway._sleep = wait_forever
        reconnect = asyncio.create_task(self.gateway.on_closed(4008, "rate limited"))
        await wait_started.wait()

        await self.gateway.close()
        await reconnect

        self.assertEqual([], self.connection.sessions)


class ConnectionSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_shard_restarts_while_other_shard_is_running(self):
        reconnected = asyncio.Event()
        calls = {"a": 0, "b": 0}
        pool = None

        async def connect(session):
            calls[session] += 1
            if session == "a" and calls[session] == 1:
                pool.add(session)
                return
            if session == "a":
                reconnected.set()
                return
            await reconnected.wait()

        pool = ConnectionSession(
            max_async=2,
            connect=connect,
            dispatch=lambda *args: None,
            loop=asyncio.get_running_loop(),
        )
        pool.add("a")
        pool.add("b")

        await asyncio.wait_for(pool.multi_run(session_interval=0), timeout=1)

        self.assertEqual(2, calls["a"])
        self.assertEqual(1, calls["b"])

    async def test_reconnect_delay_is_not_followed_by_start_window_delay(self):
        calls = 0
        pool = None

        async def connect(session):
            nonlocal calls
            calls += 1
            if calls == 1:
                pool.add(session, is_reconnect=True)

        pool = ConnectionSession(
            max_async=1,
            connect=connect,
            dispatch=lambda *args: None,
            loop=asyncio.get_running_loop(),
        )
        pool.add("session")

        with unittest.mock.patch("botpy.connection.asyncio.sleep", new=AsyncMock()) as sleep:
            await pool.multi_run(session_interval=5)

        self.assertEqual(2, calls)
        sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
