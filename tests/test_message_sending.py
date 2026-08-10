import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from botpy.api import BotAPI
from botpy.client import Client
from botpy.protocol import MediaFileType, MediaSendResult, MessageType, ReplyLimiter, ReplyTarget, UploadCache


class RecordingApi:
    def __init__(self):
        self.calls = []

    async def post_c2c_message(self, target_id, **kwargs):
        self.calls.append(("c2c_message", target_id, kwargs))
        return {"id": "c2c-message"}

    async def post_group_message(self, target_id, **kwargs):
        self.calls.append(("group_message", target_id, kwargs))
        return {"id": "group-message"}

    async def post_message(self, target_id, **kwargs):
        self.calls.append(("channel_message", target_id, kwargs))
        return {"id": "channel-message"}

    async def post_dms(self, target_id, **kwargs):
        self.calls.append(("dm_message", target_id, kwargs))
        return {"id": "dm-message"}

    async def post_c2c_file(self, target_id, **kwargs):
        self.calls.append(("c2c_file", target_id, kwargs))
        return {"file_uuid": "uuid", "file_info": "c2c-info", "ttl": 60}

    async def post_group_file(self, target_id, **kwargs):
        self.calls.append(("group_file", target_id, kwargs))
        return {"file_uuid": "uuid", "file_info": "group-info", "ttl": 60}

    async def recall_c2c_message(self, target_id, message_id):
        self.calls.append(("recall_c2c", target_id, message_id))

    async def recall_group_message(self, target_id, message_id):
        self.calls.append(("recall_group", target_id, message_id))

    async def recall_message(self, target_id, message_id, **kwargs):
        self.calls.append(("recall_channel", target_id, message_id, kwargs))


def make_client(api=None):
    return type("DummyClient", (), {"api": api or RecordingApi()})()


class UniversalMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_infers_message_types_and_increments_reply_sequence(self):
        dummy = make_client()
        target = ReplyTarget(
            scope="c2c",
            target_id="user",
            message_id="inbound",
            event_id="event",
        )

        await Client.send(dummy, target, markdown={"content": "# hi"})
        await Client.send(dummy, target, ark={"template_id": 23, "kv": []})
        await Client.send(dummy, target, media={"file_info": "info"})

        calls = dummy.api.calls
        self.assertEqual([2, 3, 7], [call[2]["msg_type"] for call in calls])
        self.assertEqual([1, 2, 3], [call[2]["msg_seq"] for call in calls])
        self.assertEqual("event", calls[0][2]["event_id"])

    async def test_send_extra_can_expose_new_platform_fields(self):
        dummy = make_client()
        target = ReplyTarget(scope="c2c", target_id="user", message_id="inbound")

        await Client.send(
            dummy,
            target,
            content="wake up",
            extra={"is_wakeup": True, "msg_seq": 99, "future_field": {"enabled": True}},
        )

        payload = dummy.api.calls[0][2]
        self.assertEqual(0, payload["msg_type"])
        self.assertEqual(99, payload["msg_seq"])
        self.assertTrue(payload["is_wakeup"])
        self.assertEqual({"enabled": True}, payload["future_field"])

    async def test_channel_and_dm_do_not_send_c2c_msg_type(self):
        dummy = make_client()

        await Client.send_markdown(
            dummy,
            ReplyTarget(scope="channel", target_id="channel"),
            "# channel",
        )
        await Client.send_text(
            dummy,
            ReplyTarget(scope="dm", target_id="guild", message_id="message"),
            "hello",
        )

        self.assertNotIn("msg_type", dummy.api.calls[0][2])
        self.assertEqual({"content": "# channel"}, dummy.api.calls[0][2]["markdown"])
        self.assertNotIn("msg_type", dummy.api.calls[1][2])
        self.assertEqual("message", dummy.api.calls[1][2]["msg_id"])

    async def test_markdown_keyboard_and_wakeup_helpers(self):
        dummy = make_client()
        target = ReplyTarget(scope="c2c", target_id="user")
        keyboard = {"content": {"rows": []}}

        await Client.send_markdown(dummy, target, "# hi", keyboard)
        await Client.send_text_with_keyboard(dummy, target, "choose", keyboard)
        await Client.send_wakeup(dummy, target, "new message")

        self.assertEqual(MessageType.MARKDOWN, dummy.api.calls[0][2]["msg_type"])
        self.assertEqual(keyboard, dummy.api.calls[0][2]["keyboard"])
        self.assertEqual(MessageType.TEXT, dummy.api.calls[1][2]["msg_type"])
        self.assertTrue(dummy.api.calls[2][2]["is_wakeup"])
        with self.assertRaises(ValueError):
            await Client.send_wakeup(dummy, ReplyTarget(scope="group", target_id="group"), "no")

    async def test_recall_routes_confirmed_scopes_and_rejects_dm(self):
        dummy = make_client()

        await Client.recall_message(dummy, ReplyTarget(scope="c2c", target_id="user"), "one")
        await Client.recall_message(dummy, ReplyTarget(scope="group", target_id="group"), "two")
        await Client.recall_message(
            dummy,
            ReplyTarget(scope="channel", target_id="channel"),
            "three",
            hidetip=True,
        )

        self.assertEqual("recall_c2c", dummy.api.calls[0][0])
        self.assertEqual("recall_group", dummy.api.calls[1][0])
        self.assertEqual({"hidetip": True}, dummy.api.calls[2][3])
        with self.assertRaises(ValueError):
            await Client.recall_message(dummy, ReplyTarget(scope="dm", target_id="guild"), "four")

    async def test_passive_reply_limit_falls_back_to_proactive_message(self):
        dummy = make_client()
        dummy._reply_limiter = ReplyLimiter(limit=1)
        target = ReplyTarget(scope="c2c", target_id="user", message_id="inbound", event_id="event")

        await Client.send_text(dummy, target, "first")
        await Client.send_text(dummy, target, "second")

        self.assertEqual("inbound", dummy.api.calls[0][2]["msg_id"])
        self.assertNotIn("msg_id", dummy.api.calls[1][2])
        self.assertNotIn("event_id", dummy.api.calls[1][2])
        self.assertNotIn("msg_seq", dummy.api.calls[1][2])

    async def test_send_text_chunks_and_honors_markdown_support(self):
        dummy = make_client()
        dummy._markdown_support = True

        results = await Client.send_text(dummy, ReplyTarget(scope="group", target_id="group"), "x" * 5001)

        self.assertEqual(2, len(results))
        self.assertEqual([5000, 1], [len(call[2]["markdown"]["content"]) for call in dummy.api.calls])
        self.assertEqual([2, 2], [call[2]["msg_type"] for call in dummy.api.calls])

    async def test_message_sent_hook_receives_ref_idx_and_metadata(self):
        class Api(RecordingApi):
            async def post_c2c_message(self, target_id, **kwargs):
                self.calls.append(("c2c_message", target_id, kwargs))
                return {"id": "message", "ext_info": {"ref_idx": "ref-1"}}

        captured = []
        dummy = make_client(Api())
        dummy._message_sent_hook = lambda ref_idx, meta: captured.append((ref_idx, meta))

        await Client.send_text(dummy, ReplyTarget(scope="c2c", target_id="user"), "hello")

        self.assertEqual("ref-1", captured[0][0])
        self.assertEqual("c2c", captured[0][1]["scope"])


class MediaMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_bytes_encodes_base64_and_routes_by_scope(self):
        dummy = make_client()

        result = await Client.upload_media(
            dummy,
            ReplyTarget(scope="c2c", target_id="user"),
            MediaFileType.IMAGE,
            data=b"image-bytes",
        )

        self.assertEqual("c2c-info", result["file_info"])
        call = dummy.api.calls[0]
        self.assertEqual("c2c_file", call[0])
        self.assertEqual(base64.b64encode(b"image-bytes").decode("ascii"), call[2]["file_data"])
        self.assertEqual(1, call[2]["file_type"])

    async def test_upload_local_file_sets_and_sanitizes_file_name(self):
        dummy = make_client()
        with tempfile.TemporaryDirectory() as directory:
            local_path = Path(directory) / "report.txt"
            local_path.write_bytes(b"hello")
            await Client.upload_media(
                dummy,
                ReplyTarget(scope="group", target_id="group"),
                MediaFileType.FILE,
                local_path=local_path,
                file_name='bad/name?.txt',
            )

        payload = dummy.api.calls[0][2]
        self.assertEqual("bad_name_.txt", payload["file_name"])
        self.assertEqual(base64.b64encode(b"hello").decode("ascii"), payload["file_data"])

    async def test_send_media_returns_upload_and_message_results(self):
        dummy = make_client()
        target = ReplyTarget(scope="group", target_id="group", message_id="inbound")

        result = await Client.send_image(dummy, target, url="https://example.com/image.png", content="caption")

        self.assertIsInstance(result, MediaSendResult)
        self.assertEqual("group-info", result.upload["file_info"])
        self.assertEqual("group-message", result.message["id"])
        self.assertEqual("group_file", dummy.api.calls[0][0])
        payload = dummy.api.calls[1][2]
        self.assertEqual(7, payload["msg_type"])
        self.assertEqual({"file_info": "group-info"}, payload["media"])
        self.assertEqual("caption", payload["content"])

    async def test_upload_validation_rejects_unsupported_or_ambiguous_sources(self):
        dummy = make_client()
        c2c = ReplyTarget(scope="c2c", target_id="user")

        with self.assertRaises(ValueError):
            await Client.upload_media(dummy, c2c, MediaFileType.IMAGE)
        with self.assertRaises(ValueError):
            await Client.upload_media(
                dummy,
                c2c,
                MediaFileType.IMAGE,
                url="https://example.com/image.png",
                data=b"duplicate",
            )
        with self.assertRaises(ValueError):
            await Client.upload_media(
                dummy,
                ReplyTarget(scope="channel", target_id="channel"),
                MediaFileType.IMAGE,
                data=b"image",
            )
        with patch("botpy.client.MAX_MEDIA_UPLOAD_SIZE", 3):
            with self.assertRaises(ValueError):
                await Client.upload_media(dummy, c2c, MediaFileType.IMAGE, data=b"1234")

    async def test_upload_cache_reuses_file_info_for_identical_content(self):
        dummy = make_client()
        dummy._upload_cache = UploadCache()
        target = ReplyTarget(scope="c2c", target_id="user")

        first = await Client.upload_media(dummy, target, MediaFileType.IMAGE, data=b"image-bytes")
        second = await Client.upload_media(dummy, target, MediaFileType.IMAGE, data=b"image-bytes")

        self.assertEqual(first["file_info"], second["file_info"])
        self.assertTrue(second["cached"])
        self.assertEqual(1, len(dummy.api.calls))


class BotApiMessageRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_c2c_recall_route(self):
        class Http:
            async def request(inner_self, route, **kwargs):
                inner_self.route = route
                inner_self.kwargs = kwargs

        http = Http()
        await BotAPI(http).recall_c2c_message("user", "message")

        self.assertEqual("DELETE", http.route.method)
        self.assertEqual("/v2/users/{openid}/messages/{message_id}", http.route.path)
        self.assertEqual("https://api.sgroup.qq.com/v2/users/user/messages/message", http.route.url)

    async def test_file_upload_includes_file_name_and_omits_none(self):
        class Http:
            async def request(inner_self, route, **kwargs):
                inner_self.route = route
                inner_self.payload = kwargs["json"]
                return {"file_info": "info"}

        http = Http()
        await BotAPI(http).post_group_file(
            "group",
            file_type=4,
            file_data="Zm9v",
            file_name="file.txt",
        )

        self.assertEqual("/v2/groups/{group_openid}/files", http.route.path)
        self.assertEqual("file.txt", http.payload["file_name"])
        self.assertNotIn("url", http.payload)

    async def test_raw_extra_fields_are_flattened(self):
        class Http:
            async def request(inner_self, route, **kwargs):
                inner_self.payload = kwargs["json"]
                return {"id": "message"}

        http = Http()
        await BotAPI(http).post_c2c_message(
            "user",
            content="hello",
            is_wakeup=True,
            future_field={"enabled": True},
        )

        self.assertTrue(http.payload["is_wakeup"])
        self.assertEqual({"enabled": True}, http.payload["future_field"])
        self.assertNotIn("extra", http.payload)


if __name__ == "__main__":
    unittest.main()
