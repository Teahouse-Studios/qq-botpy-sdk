import base64
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
    MediaUrlResult,
    ReplyTarget,
    UploadCache,
    UploadDailyLimitExceededError,
)
from botpy.protocol.media import _parse_prepare_response


class ChunkedApi:
    def __init__(self, block_size=4, concurrency=2):
        self.block_size = block_size
        self.concurrency = concurrency
        self.raw_url = None
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
        response = {"file_uuid": "uuid", "file_info": "info", "ttl": 60}
        if self.raw_url is not None:
            response["raw_url"] = self.raw_url
        return response


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

    async def test_prepare_response_accepts_string_block_size_and_nested_upload_config(self):
        class NestedConfigApi(ChunkedApi):
            async def post_upload_prepare(inner_self, scope, target_id, **payload):
                response = await super().post_upload_prepare(scope, target_id, **payload)
                return {
                    "upload_id": response["upload_id"],
                    "block_size": str(response["block_size"]),
                    "parts": response["parts"],
                    "upload_config": {
                        "concurrency": response["concurrency"],
                        "retry_timeout": response["retry_timeout"],
                    },
                }

        api = NestedConfigApi(block_size=4, concurrency=1)
        result = await ChunkedMediaUploader(api).upload(
            "c2c",
            "user",
            MediaFileType.IMAGE,
            data=b"12345678",
        )

        self.assertEqual("info", result["file_info"])
        self.assertEqual([b"1234", b"5678"], [put[1] for put in api.puts])
        self.assertEqual([1, 2], [finish[2]["part_index"] for finish in api.finishes])

    def test_parse_prepare_response_normalizes_strings_nesting_and_zero_based_indexes(self):
        upload_id, block_size, parts, concurrency, retry_timeout = _parse_prepare_response(
            {
                "upload_id": "upload",
                "block_size": "4",
                "parts": [
                    {"index": 0, "presigned_url": "https://cos.example/zero"},
                    {"index": 1, "presigned_url": "https://cos.example/one"},
                ],
                "upload_config": {"concurrency": "3", "retry_timeout": "30"},
            },
            file_size=8,
        )

        self.assertEqual("upload", upload_id)
        self.assertEqual(4, block_size)
        self.assertEqual([1, 2], [part.index for part in parts])
        self.assertEqual("https://cos.example/zero", parts[0].presigned_url)
        self.assertEqual(3, concurrency)
        self.assertEqual(30.0, retry_timeout)

    def test_parse_prepare_response_keeps_one_based_indexes_and_top_level_config(self):
        _, block_size, parts, concurrency, retry_timeout = _parse_prepare_response(
            {
                "upload_id": "upload",
                "block_size": 4,
                "parts": [
                    {"index": 1, "presigned_url": "https://cos.example/one"},
                    {"index": 2, "presigned_url": "https://cos.example/two"},
                ],
                "concurrency": 4,
                "retry_timeout": 7,
            },
            file_size=8,
        )

        self.assertEqual(4, block_size)
        self.assertEqual([1, 2], [part.index for part in parts])
        self.assertEqual(4, concurrency)
        self.assertEqual(7.0, retry_timeout)

    def test_parse_prepare_response_rejects_invalid_fields(self):
        base = {
            "upload_id": "upload",
            "block_size": 4,
            "parts": [
                {"index": 0, "presigned_url": "https://cos.example/zero"},
                {"index": 1, "presigned_url": "https://cos.example/one"},
            ],
        }

        for response in (
            {**base, "block_size": "ten"},
            {**base, "block_size": "0"},
            {**base, "parts": [{"index": 2, "presigned_url": "https://cos.example/two"}]},
            {
                **base,
                "parts": [
                    {"index": 0, "presigned_url": "https://cos.example/zero"},
                    {"index": 2, "presigned_url": "https://cos.example/two"},
                ],
            },
            {
                **base,
                "parts": [
                    {"index": 0, "presigned_url": "https://cos.example/zero"},
                    {"index": 0, "presigned_url": "https://cos.example/duplicate"},
                ],
            },
            {
                **base,
                "parts": [
                    {"index": 0, "presigned_url": "https://cos.example/zero"},
                    {"index": 1, "presigned_url": "ftp://cos.example/one"},
                ],
            },
        ):
            with self.subTest(response=response), self.assertRaises(ValueError):
                _parse_prepare_response(response, file_size=8)

    async def test_zero_based_part_indexes_are_normalized_before_finish(self):
        class ZeroBasedApi(ChunkedApi):
            async def post_upload_prepare(inner_self, scope, target_id, **payload):
                response = await super().post_upload_prepare(scope, target_id, **payload)
                response["parts"] = [
                    {"index": part["index"] - 1, "presigned_url": part["presigned_url"]} for part in response["parts"]
                ]
                return response

        api = ZeroBasedApi(block_size=4)
        await ChunkedMediaUploader(api).upload("c2c", "user", MediaFileType.IMAGE, data=b"abcdefghij")

        self.assertEqual([1, 2, 3], [finish[2]["part_index"] for finish in api.finishes])
        self.assertEqual({b"abcd", b"efgh", b"ij"}, {put[1] for put in api.puts})
        self.assertEqual(
            {"https://cos.example/part-1", "https://cos.example/part-2", "https://cos.example/part-3"},
            {put[0] for put in api.puts},
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

    async def test_force_chunked_sends_small_media_through_the_chunked_protocol(self):
        api = ChunkedApi(block_size=4, concurrency=1)
        dummy = type("DummyClient", (), {"api": api})()
        target = ReplyTarget(scope="c2c", target_id="user")

        result = await Client.upload_media(dummy, target, MediaFileType.IMAGE, data=b"abcd", force_chunked=True)

        self.assertEqual("info", result["file_info"])
        self.assertEqual([b"abcd"], [put[1] for put in api.puts])
        self.assertEqual(4, api.prepare_payload[2]["file_size"])
        self.assertEqual(1, api.finishes[0][2]["part_index"])

    async def test_force_chunked_supports_local_file_and_base64_sources(self):
        target = ReplyTarget(scope="c2c", target_id="user")
        api = ChunkedApi(block_size=4, concurrency=1)
        dummy = type("DummyClient", (), {"api": api})()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.txt"
            path.write_bytes(b"123456")
            result = await Client.upload_media(dummy, target, MediaFileType.FILE, local_path=path, force_chunked=True)

        self.assertEqual("info", result["file_info"])
        self.assertEqual([b"1234", b"56"], [put[1] for put in api.puts])
        self.assertEqual("note.txt", api.prepare_payload[2]["file_name"])

        encoded_api = ChunkedApi(block_size=4, concurrency=1)
        encoded_client = type("DummyClient", (), {"api": encoded_api})()
        payload = base64.b64encode(b"123456").decode("ascii")
        encoded_result = await Client.upload_media(
            encoded_client,
            target,
            MediaFileType.IMAGE,
            file_data=payload,
            force_chunked=True,
        )

        self.assertEqual("info", encoded_result["file_info"])
        self.assertEqual([b"1234", b"56"], [put[1] for put in encoded_api.puts])
        self.assertEqual(hashlib.md5(b"123456").hexdigest(), encoded_api.prepare_payload[2]["md5"])

    async def test_force_chunked_rejects_url_source_and_non_boolean(self):
        dummy = type("DummyClient", (), {"api": ChunkedApi()})()
        target = ReplyTarget(scope="c2c", target_id="user")

        with self.assertRaises(ValueError):
            await Client.upload_media(
                dummy,
                target,
                MediaFileType.IMAGE,
                url="https://example.com/image.png",
                force_chunked=True,
            )
        with self.assertRaises(TypeError):
            await Client.upload_media(dummy, target, MediaFileType.IMAGE, data=b"a", force_chunked="yes")
        with self.assertRaises(ValueError):
            await Client.upload_media_url(
                dummy,
                target,
                MediaFileType.IMAGE,
                url="https://example.com/image.png",
            )

    async def test_upload_media_url_returns_raw_url_and_reuses_the_cache(self):
        api = ChunkedApi(block_size=3, concurrency=1)
        api.raw_url = "https://multimedia.example/raw"
        dummy = type("DummyClient", (), {"api": api, "_upload_cache": UploadCache()})()
        target = ReplyTarget(scope="group", target_id="group")

        first = await Client.upload_media_url(dummy, target, MediaFileType.IMAGE, data=b"1234")
        second = await Client.upload_media_url(dummy, target, MediaFileType.IMAGE, data=b"1234")

        self.assertIsInstance(first, MediaUrlResult)
        self.assertEqual("https://multimedia.example/raw", first.raw_url)
        self.assertEqual(60, first.ttl)
        self.assertEqual("info", first.upload["file_info"])
        self.assertEqual("https://multimedia.example/raw", second.raw_url)
        self.assertTrue(second.upload["cached"])
        self.assertEqual([b"123", b"4"], [put[1] for put in api.puts])

    async def test_upload_media_url_requires_platform_raw_url(self):
        dummy = type("DummyClient", (), {"api": ChunkedApi(), "_upload_cache": UploadCache()})()
        target = ReplyTarget(scope="c2c", target_id="user")

        with self.assertRaises(RuntimeError):
            await Client.upload_media_url(dummy, target, MediaFileType.IMAGE, data=b"12")

    async def test_upload_media_url_ignores_cached_file_info_without_raw_url(self):
        class OneShotApi(ChunkedApi):
            async def post_c2c_file(inner_self, target_id, **kwargs):
                inner_self.one_shot = kwargs
                return {"file_uuid": "uuid", "file_info": "info", "ttl": 60}

        api = OneShotApi(block_size=2, concurrency=1)
        api.raw_url = "https://multimedia.example/raw"
        dummy = type("DummyClient", (), {"api": api, "_upload_cache": UploadCache()})()
        target = ReplyTarget(scope="c2c", target_id="user")

        one_shot = await Client.upload_media(dummy, target, MediaFileType.IMAGE, data=b"12")
        self.assertNotIn("raw_url", one_shot)
        direct = await Client.upload_media_url(dummy, target, MediaFileType.IMAGE, data=b"12")

        self.assertEqual("https://multimedia.example/raw", direct.raw_url)
        self.assertEqual([b"12"], [put[1] for put in api.puts])

    async def test_cached_chunked_response_reuses_raw_url_and_full_fields(self):
        api = ChunkedApi(block_size=2, concurrency=1)
        api.raw_url = "https://multimedia.example/raw"
        dummy = type("DummyClient", (), {"api": api, "_upload_cache": UploadCache()})()
        target = ReplyTarget(scope="c2c", target_id="user")

        await Client.upload_media_url(dummy, target, MediaFileType.IMAGE, data=b"12")
        cached = await Client.upload_media(dummy, target, MediaFileType.IMAGE, data=b"12")

        self.assertTrue(cached["cached"])
        self.assertEqual("info", cached["file_info"])
        self.assertEqual("uuid", cached["file_uuid"])
        self.assertEqual("https://multimedia.example/raw", cached["raw_url"])
        self.assertGreater(cached["ttl"], 0)


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

    async def test_part_finish_always_sends_one_based_numeric_payload(self):
        class Http:
            def __init__(inner_self):
                inner_self.calls = []

            async def request(inner_self, route, **kwargs):
                inner_self.calls.append((route, kwargs))
                return {"ok": True}

        http = Http()
        await BotAPI(http).post_upload_part_finish(
            "c2c",
            "user",
            upload_id="upload",
            part_index=1,
            block_size="10",
            md5="part-md5",
        )

        payload = http.calls[0][1]["json"]
        self.assertEqual({"upload_id": "upload", "part_index": 1, "block_size": 10, "md5": "part-md5"}, payload)
        self.assertIsInstance(payload["part_index"], int)
        self.assertIsInstance(payload["block_size"], int)

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
