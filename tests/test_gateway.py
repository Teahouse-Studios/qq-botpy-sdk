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

    async def get_access_token(self):
        return self.access_token

    def get_string(self):
        return "Bot appid.%s" % self.access_token

    def clear_access_token(self, stale_token):
        if self.access_token != stale_token:
            return False
        self.access_token = None
        return True


class DummyConnection:
    def __init__(self, loop, parser=None):
        self.loop = loop
        self.parser = parser or {}
        self.sessions = []
        self.ready_sessions = []
        self.disconnected_sessions = []
        self.gateway_owners = []

    def add(self, session, *, is_reconnect=False):
        self.sessions.append(session)

    def claim_gateway(self, session, owner):
        self.gateway_owners.append((session, owner))

    def mark_ready(self, session, *, owner=None):
        self.ready_sessions.append(session)
        return True

    def mark_disconnected(self, session, *, owner=None):
        self.disconnected_sessions.append(session)
        return True


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

    async def test_ready_and_close_update_gateway_availability(self):
        self.gateway._start_heartbeat = lambda: None
        self.connection.parser["ready"] = lambda payload: None

        await self.gateway.on_message(
            DummyWebSocket(),
            json.dumps(
                {
                    "op": 0,
                    "s": 1,
                    "t": "READY",
                    "d": {
                        "version": 1,
                        "session_id": "new-session",
                        "shard": [0, 1],
                        "user": {"username": "bot"},
                    },
                }
            ),
        )
        await self.gateway.on_closed(1000, "normal")

        self.assertEqual([self.session], self.connection.ready_sessions)
        self.assertEqual([self.session], self.connection.disconnected_sessions)

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

    async def test_replaced_gateway_cannot_save_sequence_after_awaited_handler(self):
        pool = ConnectionSession(
            max_async=1,
            connect=AsyncMock(),
            dispatch=lambda *args: None,
            loop=asyncio.get_running_loop(),
        )
        session = make_session()
        store = MemorySessionStore()
        session["session_store"] = store
        pool.add(session)
        pool._session_list.clear()
        old_gateway = BotWebSocket(session, pool)
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()

        async def handle_event(event):
            handler_started.set()
            await release_handler.wait()

        old_gateway._event_handler = handle_event
        handling = asyncio.create_task(
            old_gateway.on_message(
                DummyWebSocket(),
                json.dumps({"op": 0, "s": 99, "t": "FUTURE_EVENT", "d": {}}),
            )
        )
        await handler_started.wait()
        BotWebSocket(session, pool)
        release_handler.set()
        await handling

        self.assertIsNone(session["last_seq"])
        self.assertIsNone(await store.load("app-id", 0))

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
        self.assertEqual("token", self.session["token"].access_token)
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

    async def test_invalid_sequence_close_clears_session_but_preserves_access_token(self):
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
        self.assertEqual("token", self.session["token"].access_token)
        self.assertEqual(1, len(self.connection.sessions))
        self.assertIsNone(await store.load("app-id", 0))

    async def test_auth_failure_does_not_clear_a_newer_access_token(self):
        self.gateway.send_msg = AsyncMock()
        await self.gateway.ws_identify()
        self.session["token"].access_token = "new-token"

        await self.gateway.on_closed(4004, "old token rejected")

        self.assertEqual("new-token", self.session["token"].access_token)
        self.assertEqual(1, len(self.connection.sessions))

    async def test_replaced_gateway_close_cannot_clear_session_or_reconnect(self):
        pool = ConnectionSession(
            max_async=1,
            connect=AsyncMock(),
            dispatch=lambda *args: None,
            loop=asyncio.get_running_loop(),
        )
        session = make_session()
        pool.add(session)
        pool._session_list.clear()
        old_gateway = BotWebSocket(session, pool)
        replacement_gateway = BotWebSocket(session, pool)
        pool.mark_ready(session, owner=replacement_gateway)
        old_gateway._sleep = AsyncMock()

        await old_gateway.on_closed(4007, "late close")

        self.assertTrue(pool.is_ready)
        self.assertEqual("session-id", session["session_id"])
        self.assertEqual([], pool._session_list)
        old_gateway._sleep.assert_not_awaited()

    async def test_replaced_gateway_ignores_late_ready_event(self):
        pool = ConnectionSession(
            max_async=1,
            connect=AsyncMock(),
            dispatch=lambda *args: None,
            loop=asyncio.get_running_loop(),
        )
        session = make_session()
        pool.add(session)
        pool._session_list.clear()
        old_gateway = BotWebSocket(session, pool)
        replacement_gateway = BotWebSocket(session, pool)
        pool.mark_ready(session, owner=replacement_gateway)

        await old_gateway.on_message(
            DummyWebSocket(),
            json.dumps(
                {
                    "op": 0,
                    "s": 99,
                    "t": "READY",
                    "d": {
                        "version": 1,
                        "session_id": "stale-session",
                        "shard": [0, 2],
                        "user": {"username": "stale-bot"},
                    },
                }
            ),
        )

        self.assertTrue(pool.is_ready)
        self.assertEqual("session-id", session["session_id"])
        self.assertIsNone(session["last_seq"])
        self.assertEqual({"shard_id": 0, "shard_count": 1}, session["shards"])
        self.assertIsNone(old_gateway._heartbeat_task)

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

    async def test_duplicate_close_does_not_cancel_heartbeat_owned_reconnect(self):
        wait_started = asyncio.Event()
        release_wait = asyncio.Event()

        async def controlled_sleep(delay):
            wait_started.set()
            await release_wait.wait()

        self.gateway._sleep = controlled_sleep
        heartbeat = asyncio.create_task(self.gateway._queue_reconnect(custom_delay=1))
        self.gateway._heartbeat_task = heartbeat
        await wait_started.wait()

        await self.gateway.on_closed(4000, "duplicate receive-loop close")

        self.assertFalse(heartbeat.done())
        release_wait.set()
        await heartbeat
        self.assertEqual([self.session], self.connection.sessions)


class ConnectionSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_ready_requires_every_registered_shard(self):
        pool = ConnectionSession(
            max_async=2,
            connect=AsyncMock(),
            dispatch=lambda *args: None,
            loop=asyncio.get_running_loop(),
        )
        first = {"shards": {"shard_id": 0, "shard_count": 2}}
        second = {"shards": {"shard_id": 1, "shard_count": 2}}
        pool.add(first)
        pool.add(second)

        pool.mark_ready(first)
        self.assertFalse(pool.is_ready)
        waiter = asyncio.create_task(pool.wait_until_ready(timeout=1))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        pool.mark_ready(second)
        await waiter
        self.assertTrue(pool.is_ready)

        pool.mark_disconnected(first)
        self.assertFalse(pool.is_ready)

    async def test_waiter_rechecks_readiness_after_set_clear_race(self):
        pool = ConnectionSession(
            max_async=1,
            connect=AsyncMock(),
            dispatch=lambda *args: None,
            loop=asyncio.get_running_loop(),
        )
        shard = {"shards": {"shard_id": 0, "shard_count": 1}}
        pool.add(shard)
        waiter = asyncio.create_task(pool.wait_until_ready(timeout=1))
        await asyncio.sleep(0)

        pool.mark_ready(shard)
        pool.mark_disconnected(shard)
        await asyncio.sleep(0)

        self.assertFalse(waiter.done())
        pool.mark_ready(shard)
        await waiter

    async def test_stale_gateway_cannot_clear_replacement_readiness(self):
        pool = ConnectionSession(
            max_async=1,
            connect=AsyncMock(),
            dispatch=lambda *args: None,
            loop=asyncio.get_running_loop(),
        )
        shard = {"shards": {"shard_id": 0, "shard_count": 1}}
        old_gateway = object()
        replacement_gateway = object()
        pool.add(shard)
        pool.claim_gateway(shard, old_gateway)
        pool.mark_ready(shard, owner=old_gateway)
        pool.claim_gateway(shard, replacement_gateway)
        pool.mark_ready(shard, owner=replacement_gateway)

        pool.mark_disconnected(shard, owner=old_gateway)

        self.assertTrue(pool.is_ready)
        pool.mark_disconnected(shard, owner=replacement_gateway)
        self.assertFalse(pool.is_ready)

    async def test_close_wakes_waiters_and_rejects_reconnect_enqueue(self):
        connect = AsyncMock()
        pool = ConnectionSession(
            max_async=1,
            connect=connect,
            dispatch=lambda *args: None,
            loop=asyncio.get_running_loop(),
        )
        shard = {"shards": {"shard_id": 0, "shard_count": 1}}
        pool.add(shard)
        waiter = asyncio.create_task(pool.wait_until_ready(timeout=None))
        await asyncio.sleep(0)

        pool.mark_closed()
        pool.add(shard, is_reconnect=True)

        with self.assertRaises(RuntimeError):
            await waiter
        await pool.multi_run(session_interval=0)
        connect.assert_not_awaited()

    async def test_close_cancels_running_gateway_tasks(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def connect(session):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        pool = ConnectionSession(
            max_async=1,
            connect=connect,
            dispatch=lambda *args: None,
            loop=asyncio.get_running_loop(),
        )
        pool.add("session")
        runner = asyncio.create_task(pool.multi_run(session_interval=0))
        await started.wait()

        pool.mark_closed()

        await asyncio.wait_for(runner, timeout=1)
        self.assertTrue(cancelled.is_set())

    async def test_cancelling_coordinator_cancels_gateway_tasks(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def connect(session):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        pool = ConnectionSession(
            max_async=1,
            connect=connect,
            dispatch=lambda *args: None,
            loop=asyncio.get_running_loop(),
        )
        pool.add("session")
        coordinator = asyncio.create_task(pool.multi_run(session_interval=0))
        await started.wait()

        coordinator.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await coordinator
        self.assertTrue(cancelled.is_set())

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
