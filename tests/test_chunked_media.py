import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from botpy.api import BotAPI
from botpy.client import Client
from botpy.protocol import (
    ApiError,
    ChunkedMediaUploader,
    MediaFileType,
    ReplyTarget,
    UploadDailyLimitExceededError,
)


class ChunkedApi:
    def __init__(self, block_size=4, concurrency=2):
        self.block_size = block_size
        self.concurrency = concurrency
        self.prepare_payload = None
        self.puts = []
        self.finishes = []
        self.complete = None

    async def post_upload_prepare(self, scope, target_id, **payload):
        self.prepare_payload = (scope, target_id, payload)
        part_count = (payload["file_size"] + self.block_size - 1) // self.block_size
        return {
            "upload_id": "upload-id",
            "block_size": self.block_size,
            "parts": [
                {"index": index, "presigned_url": f"https://cos.example/part-{index}"}
                for index in range(1, part_count + 1)
            ],
            "concurrency": self.concurrency,
            "retry_timeout": 5,
        }

    async def put_upload_part(self, presigned_url, data, *, timeout=300):
        self.puts.append((presigned_url, data, timeout))

    async def post_upload_part_finish(self, scope, target_id, **payload):
        self.finishes.append((scope, target_id, payload))

    async def post_upload_complete(self, scope, target_id, **payload):
        self.complete = (scope, target_id, payload)
        return {"file_uuid": "uuid", "file_info": "info", "ttl": 60}


class ChunkedMediaUploaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_bytes_upload_hashes_splits_and_completes(self):
        api = ChunkedApi(block_size=4)
        progress = []
        data = b"abcdefghij"
        uploader = ChunkedMediaUploader(api)

        result = await uploader.upload(
            "c2c",
            "user",
            MediaFileType.IMAGE,
            data=data,
            file_name="image.png",
            on_progress=lambda uploaded, total: progress.append((uploaded, total)),
        )

        self.assertEqual("info", result["file_info"])
        prepare = api.prepare_payload[2]
        self.assertEqual(hashlib.md5(data).hexdigest(), prepare["md5"])
        self.assertEqual(hashlib.sha1(data).hexdigest(), prepare["sha1"])
        self.assertEqual(prepare["md5"], prepare["md5_10m"])
        self.assertEqual([b"abcd", b"efgh", b"ij"], [put[1] for put in api.puts])
        self.assertEqual([1, 2, 3], [finish[2]["part_index"] for finish in api.finishes])
        self.assertEqual(
            [hashlib.md5(part).hexdigest() for part in (b"abcd", b"efgh", b"ij")],
            [finish[2]["md5"] for finish in api.finishes],
        )
        self.assertEqual(("c2c", "user", {"upload_id": "upload-id"}), api.complete)
        self.assertEqual((10, 10), progress[-1])

    async def test_local_file_is_read_in_parts_and_file_name_is_sanitized(self):
        api = ChunkedApi(block_size=3)
        uploader = ChunkedMediaUploader(api)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.bin"
            path.write_bytes(b"1234567")
            await uploader.upload(
                "group",
                "group-id",
                MediaFileType.FILE,
                local_path=path,
                file_name='bad/name?.bin',
            )

        self.assertEqual("bad_name_.bin", api.prepare_payload[2]["file_name"])
        self.assertEqual([b"123", b"456", b"7"], [put[1] for put in api.puts])

    async def test_part_finish_retryable_business_code_retries_until_success(self):
        class RetryApi(ChunkedApi):
            def __init__(inner_self):
                super().__init__(block_size=10, concurrency=1)
                inner_self.attempts = 0

            async def post_upload_part_finish(inner_self, scope, target_id, **payload):
                inner_self.attempts += 1
                if inner_self.attempts < 3:
                    raise ApiError("not ready", status=400, code=40093001)
                await super().post_upload_part_finish(scope, target_id, **payload)

        now = [0.0]
        sleeps = []

        async def advance(delay):
            sleeps.append(delay)
            now[0] += delay

        api = RetryApi()
        uploader = ChunkedMediaUploader(api, sleep=advance, clock=lambda: now[0])
        await uploader.upload("c2c", "user", MediaFileType.IMAGE, data=b"1234")

        self.assertEqual(3, api.attempts)
        self.assertEqual([1.0, 1.0], sleeps)

    async def test_prepare_daily_limit_has_structured_exception(self):
        class LimitedApi(ChunkedApi):
            async def post_upload_prepare(inner_self, scope, target_id, **payload):
                raise ApiError("daily limit", status=400, code=40093002)

        uploader = ChunkedMediaUploader(LimitedApi())
        with self.assertRaises(UploadDailyLimitExceededError) as caught:
            await uploader.upload("group", "group", MediaFileType.FILE, data=b"1234")

        self.assertEqual("<bytes>", caught.exception.file_path)
        self.assertEqual(4, caught.exception.file_size)

    async def test_invalid_prepare_response_and_type_size_limit_fail_fast(self):
        class InvalidApi(ChunkedApi):
            async def post_upload_prepare(inner_self, scope, target_id, **payload):
                return {
                    "upload_id": "upload",
                    "block_size": 4,
                    "parts": [{"index": 1, "presigned_url": "https://cos.example/one"}],
                }

        with self.assertRaises(ValueError):
            await ChunkedMediaUploader(InvalidApi()).upload(
                "c2c",
                "user",
                MediaFileType.IMAGE,
                data=b"12345678",
            )

        with patch.dict("botpy.protocol.media.MEDIA_FILE_SIZE_LIMITS", {1: 3}, clear=False):
            with self.assertRaises(ValueError):
                await ChunkedMediaUploader(ChunkedApi()).upload(
                    "c2c",
                    "user",
                    MediaFileType.IMAGE,
                    data=b"1234",
                )


class ClientChunkedMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_media_automatically_switches_to_chunked(self):
        api = ChunkedApi(block_size=3)
        dummy = type("DummyClient", (), {"api": api})()
        progress = []

        with patch("botpy.client.LARGE_MEDIA_THRESHOLD", 3):
            result = await Client.upload_media(
                dummy,
                ReplyTarget(scope="group", target_id="group"),
                MediaFileType.VIDEO,
                data=b"1234567",
                on_progress=lambda uploaded, total: progress.append((uploaded, total)),
            )

        self.assertEqual("info", result["file_info"])
        self.assertEqual("group", api.prepare_payload[0])
        self.assertEqual((7, 7), progress[-1])
        self.assertIsInstance(dummy._chunked_media_uploader, ChunkedMediaUploader)

    async def test_chunked_upload_rejects_srv_send_msg(self):
        dummy = type("DummyClient", (), {"api": ChunkedApi()})()
        with patch("botpy.client.LARGE_MEDIA_THRESHOLD", 3):
            with self.assertRaises(ValueError):
                await Client.upload_media(
                    dummy,
                    ReplyTarget(scope="c2c", target_id="user"),
                    MediaFileType.IMAGE,
                    data=b"1234",
                    srv_send_msg=True,
                )

    async def test_send_media_forwards_progress_then_sends_file_info(self):
        class Api(ChunkedApi):
            async def post_c2c_message(inner_self, target_id, **payload):
                inner_self.message = (target_id, payload)
                return {"id": "message"}

        api = Api(block_size=2)
        dummy = type("DummyClient", (), {"api": api})()
        progress = []
        target = ReplyTarget(scope="c2c", target_id="user", message_id="inbound")

        with patch("botpy.client.LARGE_MEDIA_THRESHOLD", 3):
            result = await Client.send_video(
                dummy,
                target,
                data=b"12345",
                content="video",
                on_progress=lambda uploaded, total: progress.append((uploaded, total)),
            )

        self.assertEqual("message", result.message["id"])
        self.assertEqual({"file_info": "info"}, api.message[1]["media"])
        self.assertEqual((5, 5), progress[-1])


class BotApiChunkedRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_finish_complete_routes(self):
        class Http:
            def __init__(inner_self):
                inner_self.calls = []

            async def request(inner_self, route, **kwargs):
                inner_self.calls.append((route, kwargs))
                return {"ok": True}

        http = Http()
        api = BotAPI(http)
        await api.post_upload_prepare(
            "c2c",
            "user",
            file_type=2,
            file_name="video.mp4",
            file_size=10,
            md5="md5",
            sha1="sha1",
            md5_10m="md5-10m",
        )
        await api.post_upload_part_finish(
            "group",
            "group",
            upload_id="upload",
            part_index=1,
            block_size=10,
            md5="part-md5",
        )
        await api.post_upload_complete("group", "group", upload_id="upload")

        self.assertEqual("/v2/users/{target_id}/upload_prepare", http.calls[0][0].path)
        self.assertEqual("/v2/groups/{target_id}/upload_part_finish", http.calls[1][0].path)
        self.assertEqual("/v2/groups/{target_id}/files", http.calls[2][0].path)
        self.assertEqual({"upload_id": "upload"}, http.calls[2][1]["json"])

    async def test_presigned_put_is_unauthenticated_raw_bytes(self):
        class Http:
            async def request_url(inner_self, method, url, **kwargs):
                inner_self.call = (method, url, kwargs)
                return ""

        http = Http()
        await BotAPI(http).put_upload_part("https://cos.example/part", b"bytes")

        method, url, kwargs = http.call
        self.assertEqual("PUT", method)
        self.assertEqual("https://cos.example/part", url)
        self.assertFalse(kwargs["auth"])
        self.assertEqual(b"bytes", kwargs["data"])
        self.assertEqual("5", kwargs["headers"]["Content-Length"])


if __name__ == "__main__":
    unittest.main()
