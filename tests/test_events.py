import unittest

from botpy.client import Client
from botpy.protocol import normalize_inbound_message, parse_gateway_event


class GatewayEventTests(unittest.TestCase):
    def test_parse_gateway_event_preserves_envelope(self):
        payload = {
            "op": 0,
            "s": 42,
            "t": "FUTURE_EVENT",
            "id": "event-id",
            "d": {"new_field": True},
        }

        event = parse_gateway_event(payload)

        self.assertEqual("FUTURE_EVENT", event.event_type)
        self.assertEqual(42, event.sequence)
        self.assertEqual("event-id", event.event_id)
        self.assertEqual(payload, event.raw)

    def test_normalizes_all_message_scopes(self):
        cases = (
            (
                "C2C_MESSAGE_CREATE",
                {"author": {"user_openid": "user"}},
                "c2c",
                "user",
            ),
            (
                "GROUP_AT_MESSAGE_CREATE",
                {"author": {"member_openid": "member"}, "group_openid": "group"},
                "group",
                "group",
            ),
            (
                "AT_MESSAGE_CREATE",
                {"author": {"id": "author"}, "channel_id": "channel"},
                "channel",
                "channel",
            ),
            (
                "DIRECT_MESSAGE_CREATE",
                {"author": {"id": "author"}, "guild_id": "dm-guild"},
                "dm",
                "dm-guild",
            ),
        )

        for event_type, extra, scope, target_id in cases:
            with self.subTest(event_type=event_type):
                data = {"id": "message-id", "content": "hello", "timestamp": "now", **extra}
                message = normalize_inbound_message(
                    parse_gateway_event({"op": 0, "s": 1, "t": event_type, "d": data})
                )

                self.assertIsNotNone(message)
                self.assertEqual(scope, message.reply_target.scope)
                self.assertEqual(target_id, message.reply_target.target_id)
                self.assertEqual("message-id", message.reply_target.message_id)

    def test_group_message_preserves_media_and_reference_metadata(self):
        data = {
            "id": "message-id",
            "content": "voice",
            "group_openid": "group",
            "author": {"member_openid": "member", "username": "name", "bot": False},
            "attachments": [
                {
                    "url": "https://example.invalid/audio",
                    "content_type": "audio/ogg",
                    "size": 12,
                    "voice_wav_url": "https://example.invalid/audio.wav",
                    "asr_refer_text": "hello",
                }
            ],
            "message_scene": {"ext": ["msg_idx=8", "ref_msg_idx=7"]},
            "message_type": 1,
            "mentions": [{"id": "mentioned"}],
        }

        message = normalize_inbound_message(
            parse_gateway_event({"op": 0, "t": "GROUP_MESSAGE_CREATE", "d": data})
        )

        self.assertEqual("member", message.author_id)
        self.assertFalse(message.author_is_bot)
        self.assertEqual("7", message.metadata["ref_msg_idx"])
        self.assertEqual("8", message.metadata["msg_idx"])
        self.assertEqual("hello", message.attachments[0].asr_refer_text)
        self.assertEqual(data, message.raw)

    def test_non_message_event_is_not_normalized(self):
        event = parse_gateway_event({"op": 0, "t": "GUILD_CREATE", "d": {"id": "guild"}})

        self.assertIsNone(normalize_inbound_message(event))


class ClientEventIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_schedules_raw_and_normalized_callbacks(self):
        scheduled = []

        class DummyClient:
            async def on_raw_event(self, event):
                return None

            async def on_message(self, message):
                return None

            def _schedule_event(self, callback, name, value):
                scheduled.append((callback, name, value))

        event = parse_gateway_event(
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

        await Client.ws_raw_dispatch(DummyClient(), event)

        self.assertEqual(["on_raw_event", "on_message"], [item[1] for item in scheduled])
        self.assertEqual("c2c", scheduled[1][2].reply_target.scope)

    async def test_client_close_stops_transports_before_http(self):
        closed = []

        class DummyTransport:
            async def close(self):
                closed.append("websocket")

        class DummyHttp:
            async def close(self):
                closed.append("http")

        class DummyClient:
            _closed = False
            _websockets = {DummyTransport()}
            http = DummyHttp()

        client = DummyClient()
        await Client.close(client)

        self.assertTrue(client._closed)
        self.assertEqual(["websocket", "http"], closed)

    async def test_session_store_close_error_does_not_skip_http_close(self):
        closed = []

        class FailingStore:
            async def close(self):
                raise RuntimeError("store failed")

        class DummyHttp:
            async def close(self):
                closed.append("http")

        class DummyClient:
            _closed = False
            _websockets = set()
            _session_store = FailingStore()
            http = DummyHttp()

        await Client.close(DummyClient())

        self.assertEqual(["http"], closed)


if __name__ == "__main__":
    unittest.main()
