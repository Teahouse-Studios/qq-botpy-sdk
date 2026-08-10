import asyncio
import json
import unittest

from aiohttp import ClientSession

from botpy.client import Client
from botpy.flags import Intents
from botpy.protocol import EventTransport
from botpy.protocol.transport import (
    AiohttpWebhookServer,
    WebhookRequest,
    WebhookResponse,
    WebhookTransport,
    ed25519_sign,
    sign_validation_response,
    verify_webhook_signature,
)


TEST_SECRET = "DG5g3B4j9X2KOErG"
TEST_APP_ID = "11111111"


class FakeWebhookServer:
    def __init__(self):
        self.handler = None
        self.closed = False
        self.close_count = 0
        self.started = asyncio.Event()
        self.bound_port = 18080

    async def listen(self, host, port, path, handler):
        self.handler = handler
        self.host = host
        self.port = port
        self.path = path
        self.started.set()

    async def close(self):
        self.closed = True
        self.close_count += 1

    async def invoke(self, request: WebhookRequest) -> WebhookResponse:
        if self.handler is None:
            raise RuntimeError("server has not started")
        return await self.handler(request)


def make_signed_request(payload, secret=TEST_SECRET):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = "1725442341"
    signature = ed25519_sign(secret, timestamp.encode("utf-8") + body)
    return WebhookRequest(
        body=body,
        headers={
            "X-Signature-Timestamp": timestamp,
            "X-Signature-Ed25519": signature,
        },
    )


class WebhookSignatureTests(unittest.TestCase):
    def test_sign_and_verify_roundtrip(self):
        body = b'{"op":0,"d":{}}'
        timestamp = "1725442341"
        signature = ed25519_sign(TEST_SECRET, timestamp.encode("utf-8") + body)

        self.assertEqual(128, len(signature))
        self.assertTrue(
            verify_webhook_signature(
                body=body,
                timestamp=timestamp,
                signature=signature,
                bot_secret=TEST_SECRET,
            )
        )

    def test_rejects_tampered_body_wrong_secret_and_invalid_hex(self):
        body = b'{"op":0}'
        timestamp = "1725442341"
        signature = ed25519_sign(TEST_SECRET, timestamp.encode("utf-8") + body)

        self.assertFalse(
            verify_webhook_signature(
                body=b'{"op":1}',
                timestamp=timestamp,
                signature=signature,
                bot_secret=TEST_SECRET,
            )
        )
        self.assertFalse(
            verify_webhook_signature(
                body=body,
                timestamp=timestamp,
                signature=signature,
                bot_secret="wrong-secret",
            )
        )
        self.assertFalse(
            verify_webhook_signature(
                body=body,
                timestamp=timestamp,
                signature="not-hex",
                bot_secret=TEST_SECRET,
            )
        )

    def test_validation_signature_is_deterministic(self):
        result = sign_validation_response(
            plain_token="plain-token",
            event_ts="1725442341",
            bot_secret=TEST_SECRET,
        )

        self.assertEqual("plain-token", result["plain_token"])
        self.assertEqual(
            ed25519_sign(TEST_SECRET, b"1725442341plain-token"),
            result["signature"],
        )


class WebhookTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeWebhookServer()
        self.received = []

        async def handler(event):
            self.received.append(event)

        self.transport = WebhookTransport(
            TEST_APP_ID,
            TEST_SECRET,
            port=8080,
            path="/callback",
            server=self.server,
        )
        self.start_task = asyncio.create_task(self.transport.start(handler))
        await self.server.started.wait()

    async def asyncTearDown(self):
        await self.transport.close()
        await self.start_task

    async def test_implements_event_transport_and_stops_server(self):
        self.assertIsInstance(self.transport, EventTransport)
        self.assertEqual("/callback", self.server.path)

        await self.transport.close()

        self.assertTrue(self.server.closed)
        self.assertEqual(1, self.server.close_count)

        await self.transport.close()
        self.assertEqual(1, self.server.close_count)

    async def test_start_failure_closes_server(self):
        await self.transport.close()
        await self.start_task

        server = FakeWebhookServer()

        async def fail_on_started(info):
            raise RuntimeError("startup callback failed")

        transport = WebhookTransport(
            TEST_APP_ID,
            TEST_SECRET,
            server=server,
            on_started=fail_on_started,
        )

        with self.assertRaisesRegex(RuntimeError, "startup callback failed"):
            await transport.start(lambda event: asyncio.sleep(0))

        self.assertTrue(server.closed)
        self.assertEqual(1, server.close_count)

    async def test_aiohttp_server_listens_on_configured_path(self):
        await self.transport.close()
        await self.start_task

        started = asyncio.Event()
        started_info = {}
        server = AiohttpWebhookServer()

        async def on_started(info):
            started_info.update(info)
            started.set()

        transport = WebhookTransport(
            TEST_APP_ID,
            TEST_SECRET,
            host="127.0.0.1",
            port=0,
            path="/callback",
            server=server,
            on_started=on_started,
        )
        start_task = asyncio.create_task(transport.start(lambda event: asyncio.sleep(0)))

        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            url = f"http://127.0.0.1:{started_info['port']}/callback"
            payload = {
                "op": 13,
                "d": {"plain_token": "plain-token", "event_ts": "1725442341"},
            }
            async with ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    body = await response.json()

                self.assertEqual(200, response.status)
                self.assertEqual("plain-token", body["plain_token"])
                self.assertEqual("application/json", response.content_type)
        finally:
            await transport.close()
            await start_task

    async def test_handles_callback_url_validation_without_headers(self):
        response = await self.server.invoke(
            WebhookRequest(
                body=json.dumps(
                    {
                        "op": 13,
                        "d": {"plain_token": "plain-token", "event_ts": "1725442341"},
                    }
                ).encode("utf-8")
            )
        )

        payload = json.loads(response.body)
        self.assertEqual(200, response.status)
        self.assertEqual("plain-token", payload["plain_token"])
        self.assertEqual(128, len(payload["signature"]))

    async def test_rejects_invalid_json_and_missing_or_bad_signature(self):
        invalid_json = await self.server.invoke(WebhookRequest(body=b"{"))
        missing = await self.server.invoke(
            WebhookRequest(body=b'{"op":0,"t":"TEST","d":{}}')
        )
        bad_signature = await self.server.invoke(
            WebhookRequest(
                body=b'{"op":0,"t":"TEST","d":{}}',
                headers={
                    "x-signature-timestamp": "123",
                    "x-signature-ed25519": "0" * 128,
                },
            )
        )

        self.assertEqual(400, invalid_json.status)
        self.assertEqual(401, missing.status)
        self.assertEqual(401, bad_signature.status)

    async def test_dispatch_returns_ack_and_forwards_raw_event(self):
        response = await self.server.invoke(
            make_signed_request(
                {
                    "op": 0,
                    "s": 42,
                    "t": "C2C_MESSAGE_CREATE",
                    "d": {
                        "id": "message-id",
                        "content": "hello",
                        "author": {"user_openid": "user"},
                    },
                }
            )
        )
        await asyncio.sleep(0)

        self.assertEqual({"op": 12, "d": 0}, json.loads(response.body))
        self.assertEqual("C2C_MESSAGE_CREATE", self.received[0].event_type)
        self.assertEqual(42, self.received[0].sequence)

    async def test_ack_does_not_wait_for_event_handler(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_handler(event):
            started.set()
            await release.wait()

        await self.transport.close()
        await self.start_task
        self.server = FakeWebhookServer()
        self.transport = WebhookTransport(TEST_APP_ID, TEST_SECRET, server=self.server)
        self.start_task = asyncio.create_task(self.transport.start(slow_handler))
        await self.server.started.wait()

        response = await self.server.invoke(
            make_signed_request({"op": 0, "t": "FUTURE_EVENT", "d": {}})
        )
        await started.wait()

        self.assertEqual(200, response.status)
        release.set()


class ClientWebhookCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_event_uses_legacy_and_normalized_callbacks(self):
        class RecordingClient(Client):
            def __init__(self):
                super().__init__(Intents.none(), bot_log=None, transport="webhook")
                self.events = []

            async def on_c2c_message_create(self, message):
                self.events.append(("legacy", message.content))

            async def on_message(self, message):
                self.events.append(("normalized", message.content))

            async def on_raw_event(self, event):
                self.events.append(("raw", event.event_type))

        client = RecordingClient()
        event = self._event()

        await client._dispatch_transport_event(event)
        await asyncio.sleep(0)

        self.assertIn(("legacy", "hello"), client.events)
        self.assertIn(("normalized", "hello"), client.events)
        self.assertIn(("raw", "C2C_MESSAGE_CREATE"), client.events)
        await client.close()

    @staticmethod
    def _event():
        from botpy.protocol import parse_gateway_event

        return parse_gateway_event(
            {
                "op": 0,
                "t": "C2C_MESSAGE_CREATE",
                "d": {
                    "id": "message-id",
                    "content": "hello",
                    "author": {"user_openid": "user"},
                },
            }
        )


if __name__ == "__main__":
    unittest.main()
