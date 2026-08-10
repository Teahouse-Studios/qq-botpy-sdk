import asyncio
import unittest
from weakref import WeakSet

from botpy.api import BotAPI
from botpy.client import Client
from botpy.protocol import RateLimitError, ReplyTarget, StreamSession


class StreamApi:
    def __init__(self):
        self.calls = []

    async def post_c2c_stream_message(self, openid, **payload):
        self.calls.append((openid, dict(payload)))
        return {"id": "stream-id"}


class StreamSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_and_complete_share_sequence_and_advance_index(self):
        api = StreamApi()
        session = StreamSession(
            api,
            openid="user",
            msg_id="message",
            event_id="message",
            msg_seq=7,
            clock=lambda: 100.0,
        )

        await session.update("first")
        result = await session.complete()

        self.assertEqual("stream-id", result["id"])
        self.assertTrue(session.completed)
        self.assertEqual("stream-id", session.stream_message_id)
        self.assertEqual([0, 1], [call[1]["index"] for call in api.calls])
        self.assertEqual([7, 7], [call[1]["msg_seq"] for call in api.calls])
        self.assertEqual([1, 10], [call[1]["input_state"] for call in api.calls])
        self.assertNotIn("stream_msg_id", api.calls[0][1])
        self.assertEqual("stream-id", api.calls[1][1]["stream_msg_id"])

    async def test_complete_flushes_latest_throttled_text(self):
        now = [100.0]
        api = StreamApi()
        session = StreamSession(
            api,
            openid="user",
            msg_id="message",
            event_id="event",
            msg_seq=1,
            throttle_ms=500,
            clock=lambda: now[0],
        )

        await session.update("a")
        now[0] += 0.1
        await session.update("ab")
        await session.complete()
        await asyncio.sleep(0)

        self.assertEqual(["a", "ab"], [call[1]["content_raw"] for call in api.calls])
        self.assertEqual(10, api.calls[-1][1]["input_state"])

    async def test_text_arriving_during_request_gets_trailing_flush(self):
        class BlockingApi(StreamApi):
            def __init__(inner_self):
                super().__init__()
                inner_self.started = asyncio.Event()
                inner_self.release = asyncio.Event()

            async def post_c2c_stream_message(inner_self, openid, **payload):
                inner_self.calls.append((openid, dict(payload)))
                if len(inner_self.calls) == 1:
                    inner_self.started.set()
                    await inner_self.release.wait()
                return {"id": "stream-id"}

        api = BlockingApi()
        session = StreamSession(
            api,
            openid="user",
            msg_id="message",
            event_id="event",
            msg_seq=3,
            clock=lambda: 100.0,
        )

        first = asyncio.create_task(session.update("a"))
        await api.started.wait()
        await session.update("ab")
        api.release.set()
        await first
        await session.complete()

        self.assertEqual(["a", "ab", "ab"], [call[1]["content_raw"] for call in api.calls])
        self.assertEqual([1, 1, 10], [call[1]["input_state"] for call in api.calls])

    async def test_rate_limit_retries_with_new_indices(self):
        class LimitedApi:
            def __init__(inner_self):
                inner_self.calls = []

            async def post_c2c_stream_message(inner_self, openid, **payload):
                inner_self.calls.append(dict(payload))
                if len(inner_self.calls) < 3:
                    raise RateLimitError("limited", status=429, code=50002)
                return {"id": "stream-id"}

        sleeps = []

        async def record_sleep(delay):
            sleeps.append(delay)

        api = LimitedApi()
        session = StreamSession(
            api,
            openid="user",
            msg_id="message",
            event_id="event",
            msg_seq=5,
            sleep=record_sleep,
            clock=lambda: 100.0,
        )

        await session.update("hello")

        self.assertEqual([1.0, 2.0], sleeps)
        self.assertEqual([0, 1, 2], [payload["index"] for payload in api.calls])

    async def test_cancel_stops_pending_flush_and_done_frame(self):
        now = [100.0]
        api = StreamApi()
        session = StreamSession(
            api,
            openid="user",
            msg_id="message",
            event_id="event",
            msg_seq=1,
            clock=lambda: now[0],
        )

        await session.update("a")
        now[0] += 0.1
        await session.update("ab")
        session.cancel()
        self.assertIsNone(await session.complete())
        await asyncio.sleep(0)

        self.assertEqual(1, len(api.calls))


class ClientStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_stream_validates_target_and_reserves_reply_sequence(self):
        class Api(StreamApi):
            async def post_c2c_message(inner_self, openid, **payload):
                inner_self.calls.append((openid, dict(payload)))
                return {"id": "message"}

        dummy = type("DummyClient", (), {"api": Api()})()
        target = ReplyTarget(scope="c2c", target_id="user", message_id="inbound")

        stream = Client.open_stream(dummy, target)
        await stream.update("stream")
        await Client.send_text(dummy, target, "after stream")

        self.assertEqual(1, dummy.api.calls[0][1]["msg_seq"])
        self.assertEqual(2, dummy.api.calls[1][1]["msg_seq"])
        with self.assertRaises(ValueError):
            Client.open_stream(dummy, ReplyTarget(scope="group", target_id="group", message_id="id"))
        with self.assertRaises(ValueError):
            Client.open_stream(dummy, ReplyTarget(scope="c2c", target_id="user"))

    async def test_client_close_cancels_live_stream_sessions(self):
        class Http:
            async def close(inner_self):
                inner_self.closed = True

        session = StreamSession(
            StreamApi(),
            openid="user",
            msg_id="message",
            event_id="event",
            msg_seq=1,
        )
        dummy = type(
            "DummyClient",
            (),
            {
                "_closed": False,
                "_event_transport": None,
                "_stream_sessions": WeakSet((session,)),
                "_websockets": set(),
                "_session_store": None,
                "http": Http(),
            },
        )()

        await Client.close(dummy)

        self.assertTrue(session.completed)
        self.assertTrue(dummy.http.closed)


class BotApiStreamRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_route_and_payload(self):
        class Http:
            async def request(inner_self, route, **kwargs):
                inner_self.route = route
                inner_self.payload = kwargs["json"]
                return {"id": "stream"}

        http = Http()
        result = await BotAPI(http).post_c2c_stream_message(
            "user",
            input_mode="replace",
            input_state=1,
            content_type="markdown",
            content_raw="hello",
            event_id="event",
            msg_id="message",
            msg_seq=8,
            index=0,
        )

        self.assertEqual("stream", result["id"])
        self.assertEqual("/v2/users/{openid}/stream_messages", http.route.path)
        self.assertEqual("https://api.sgroup.qq.com/v2/users/user/stream_messages", http.route.url)
        self.assertNotIn("stream_msg_id", http.payload)
        self.assertEqual(8, http.payload["msg_seq"])


if __name__ == "__main__":
    unittest.main()
