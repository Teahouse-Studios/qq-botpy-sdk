import hashlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from botpy.api import BotAPI
from botpy.client import Client
from botpy.protocol.media_image import (
    MediaImageUploader,
    format_media_image_markdown,
    guess_media_image_content_type,
)
from botpy.protocol.models import ReplyTarget


def make_png(width=64, height=32):
    """构造一个只带合法 PNG 头的最小图片数据，足以让 ``parse_image_size`` 识别宽高。"""

    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\r"
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


PNG = make_png()
PNG_MD5 = hashlib.md5(PNG).hexdigest()
PNG_SHA1 = hashlib.sha1(PNG).hexdigest()


class RecordingHttp:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or ())

    async def request(self, route, **kwargs):
        self.calls.append((route, kwargs))
        if self.responses:
            return self.responses.pop(0)
        return {"ok": True}

    async def request_url(self, method, url, **kwargs):
        self.calls.append((("URL", method, url), kwargs))
        if self.responses:
            return self.responses.pop(0)
        return {"ok": True}


class MediaImageApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.http = RecordingHttp()
        self.api = BotAPI(self.http)

    def assert_last_call(self, method, path, *, json=None):
        route, kwargs = self.http.calls[-1]
        self.assertEqual(method, route.method)
        self.assertEqual(path, route.path.format_map(route.parameters))
        if json is None:
            self.assertNotIn("json", kwargs)
        else:
            self.assertEqual(json, kwargs.get("json"))

    async def test_prepare_posts_metadata_to_media_images_route(self):
        expected = {"upload_id": "up-1", "presigned_url": "https://cos.example.com/put"}
        self.http.responses.append(expected)

        result = await self.api.post_media_image_prepare(
            file_name="cake.png",
            file_size=len(PNG),
            content_type="image/png",
            md5=PNG_MD5,
            sha1=PNG_SHA1,
        )

        self.assertIs(expected, result)
        self.assert_last_call(
            "POST",
            "/v2/media-images/prepare",
            json={
                "file_name": "cake.png",
                "file_size": len(PNG),
                "content_type": "image/png",
                "md5": PNG_MD5,
                "sha1": PNG_SHA1,
            },
        )

    async def test_prepare_lowercases_digests(self):
        await self.api.post_media_image_prepare(
            file_name="cake.png",
            file_size=len(PNG),
            content_type="image/png",
            md5=PNG_MD5.upper(),
            sha1=PNG_SHA1.upper(),
        )

        self.assert_last_call(
            "POST",
            "/v2/media-images/prepare",
            json={
                "file_name": "cake.png",
                "file_size": len(PNG),
                "content_type": "image/png",
                "md5": PNG_MD5,
                "sha1": PNG_SHA1,
            },
        )

    async def test_prepare_rejects_empty_file_name_and_bad_content_type(self):
        with self.assertRaises(ValueError):
            await self.api.post_media_image_prepare(
                file_name="",
                file_size=1,
                content_type="image/png",
                md5=PNG_MD5,
                sha1=PNG_SHA1,
            )
        with self.assertRaises(ValueError):
            await self.api.post_media_image_prepare(
                file_name="cake.tiff",
                file_size=1,
                content_type="image/tiff",
                md5=PNG_MD5,
                sha1=PNG_SHA1,
            )

    async def test_prepare_rejects_oversized_file_and_bad_digest(self):
        with self.assertRaises(ValueError):
            await self.api.post_media_image_prepare(
                file_name="cake.png",
                file_size=10 * 1024 * 1024 + 1,
                content_type="image/png",
                md5=PNG_MD5,
                sha1=PNG_SHA1,
            )
        with self.assertRaises(ValueError):
            await self.api.post_media_image_prepare(
                file_name="cake.png",
                file_size=1,
                content_type="image/png",
                md5="not-a-digest",
                sha1=PNG_SHA1,
            )

    async def test_put_uploads_bytes_to_presigned_url_with_matching_content_type(self):
        await self.api.put_media_image(
            "https://cos.example.com/put?sign=abc",
            PNG,
            content_type="image/png",
        )

        (tag, kwargs) = self.http.calls[-1]
        self.assertEqual(("URL", "PUT", "https://cos.example.com/put?sign=abc"), tag)
        self.assertEqual(PNG, kwargs.get("data"))
        self.assertEqual({"Content-Type": "image/png", "Content-Length": str(len(PNG))}, kwargs.get("headers"))
        self.assertFalse(kwargs.get("auth"))

    async def test_put_rejects_non_https_url(self):
        with self.assertRaises(ValueError):
            await self.api.put_media_image("http://cos.example.com/put", PNG, content_type="image/png")

    async def test_complete_posts_upload_id_and_md5(self):
        expected = {"image_id": "img-1", "url": "qqbot-media://image/v1/token", "ttl": 7776000}
        self.http.responses.append(expected)

        result = await self.api.post_media_image_complete(upload_id="up-1", md5=PNG_MD5)

        self.assertIs(expected, result)
        self.assert_last_call(
            "POST",
            "/v2/media-images/complete",
            json={"upload_id": "up-1", "md5": PNG_MD5},
        )

    async def test_list_passes_cursor_and_omits_absent_one(self):
        await self.api.get_media_images(cursor="next-page")
        route, kwargs = self.http.calls[-1]
        self.assertEqual("GET", route.method)
        self.assertEqual("/v2/media-images", route.path)
        self.assertEqual({"cursor": "next-page"}, kwargs.get("params"))

        await self.api.get_media_images()
        route, kwargs = self.http.calls[-1]
        self.assertNotIn("params", kwargs)

    async def test_delete_uses_image_id_route(self):
        await self.api.delete_media_image("img-1")
        route, _ = self.http.calls[-1]
        self.assertEqual("DELETE", route.method)
        self.assertEqual("/v2/media-images/{image_id}/delete", route.path)
        self.assertEqual({"image_id": "img-1"}, route.parameters)

    async def test_delete_rejects_empty_image_id(self):
        with self.assertRaises(ValueError):
            await self.api.delete_media_image("")


class RecordingApi:
    def __init__(self):
        self.calls = []

    async def post_media_image_prepare(self, **payload):
        self.calls.append(("prepare", payload))
        return {"upload_id": "up-1", "presigned_url": "https://cos.example.com/put"}

    async def put_media_image(self, presigned_url, data, **kwargs):
        self.calls.append(("put", presigned_url, data, kwargs))
        return {"ok": True}

    async def post_media_image_complete(self, **payload):
        self.calls.append(("complete", payload))
        return {
            "image_id": "img-1",
            "url": "qqbot-media://image/v1/token",
            "ttl": 7776000,
            "preview_url": "https://preview.example.com/a.png",
        }


class MediaImageUploaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = RecordingApi()
        self.uploader = MediaImageUploader(self.api)

    async def test_upload_runs_prepare_put_complete_in_order(self):
        result = await self.uploader.upload(data=PNG, file_name="cake.png", alt="蛋糕")

        self.assertEqual(["prepare", "put", "complete"], [call[0] for call in self.api.calls])
        self.assertEqual(
            {
                "file_name": "cake.png",
                "file_size": len(PNG),
                "content_type": "image/png",
                "md5": PNG_MD5,
                "sha1": PNG_SHA1,
            },
            self.api.calls[0][1],
        )
        self.assertEqual(("put", "https://cos.example.com/put", PNG), self.api.calls[1][:3])
        self.assertEqual({"content_type": "image/png", "timeout": 300}, self.api.calls[1][3])
        self.assertEqual({"upload_id": "up-1", "md5": PNG_MD5}, self.api.calls[2][1])

        self.assertEqual("img-1", result.image_id)
        self.assertEqual("qqbot-media://image/v1/token", result.url)
        self.assertEqual(7776000, result.ttl)
        self.assertEqual("https://preview.example.com/a.png", result.preview_url)
        self.assertEqual("cake.png", result.file_name)
        self.assertEqual("image/png", result.content_type)
        self.assertEqual(len(PNG), result.size)
        self.assertEqual("![蛋糕 #64px #32px](qqbot-media://image/v1/token)", result.markdown)

    async def test_upload_reads_local_file_and_defaults_alt_to_file_stem(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "布丁.png"
            path.write_bytes(PNG)

            result = await self.uploader.upload(local_path=str(path))

        self.assertEqual("![布丁 #64px #32px](qqbot-media://image/v1/token)", result.markdown)
        self.assertEqual(len(PNG), self.api.calls[1][2].__len__())

    async def test_upload_uses_explicit_file_name_for_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.tmp"
            path.write_bytes(PNG)

            result = await self.uploader.upload(local_path=str(path), file_name="cake.png")

        self.assertEqual("cake.png", result.file_name)

    async def test_upload_requires_exactly_one_source(self):
        with self.assertRaises(ValueError):
            await self.uploader.upload()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cake.png"
            path.write_bytes(PNG)
            with self.assertRaises(ValueError):
                await self.uploader.upload(data=PNG, local_path=str(path))

    async def test_upload_rejects_unsupported_extension_and_empty_data(self):
        with self.assertRaises(ValueError):
            await self.uploader.upload(data=PNG, file_name="cake.tiff")
        with self.assertRaises(ValueError):
            await self.uploader.upload(data=b"", file_name="cake.png")

    async def test_upload_rejects_oversized_data(self):
        with self.assertRaises(ValueError):
            await self.uploader.upload(data=b"x" * (10 * 1024 * 1024 + 1), file_name="cake.png")

    async def test_upload_rejects_content_type_mismatch(self):
        with self.assertRaises(ValueError):
            await self.uploader.upload(data=PNG, file_name="cake.png", content_type="image/jpeg")

    async def test_upload_raises_when_prepare_or_complete_is_incomplete(self):
        class IncompletePrepare:
            async def post_media_image_prepare(self, **payload):
                return {"upload_id": "up-1"}

        with self.assertRaises(RuntimeError):
            await MediaImageUploader(IncompletePrepare()).upload(data=PNG, file_name="cake.png")

        class IncompleteComplete(RecordingApi):
            async def post_media_image_complete(self, **payload):
                return {"image_id": "img-1"}

        with self.assertRaises(RuntimeError):
            await MediaImageUploader(IncompleteComplete()).upload(data=PNG, file_name="cake.png")


class MediaImageHelperTests(unittest.TestCase):
    def test_guess_content_type_maps_supported_extensions(self):
        self.assertEqual("image/png", guess_media_image_content_type("a.PNG"))
        self.assertEqual("image/jpeg", guess_media_image_content_type("a.jpg"))
        self.assertEqual("image/jpeg", guess_media_image_content_type("a.jpeg"))
        self.assertEqual("image/gif", guess_media_image_content_type("a.gif"))
        self.assertEqual("image/webp", guess_media_image_content_type("a.webp"))
        self.assertIsNone(guess_media_image_content_type("a.bmp"))
        self.assertIsNone(guess_media_image_content_type(""))

    def test_format_markdown_without_size_has_no_dimensions(self):
        markdown = format_media_image_markdown("qqbot-media://image/v1/t", alt="图")
        self.assertEqual("![图](qqbot-media://image/v1/t)", markdown)

    def test_format_markdown_requires_url(self):
        with self.assertRaises(ValueError):
            format_media_image_markdown("")


if __name__ == "__main__":
    unittest.main()


class ClientMediaImageTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _dummy(api):
        return type("DummyClient", (), {"api": api})()

    async def test_upload_media_image_builds_uploader_once(self):
        api = RecordingApi()
        dummy = self._dummy(api)

        first = await Client.upload_media_image(dummy, data=PNG, file_name="cake.png")
        second = await Client.upload_media_image(dummy, data=PNG, file_name="cake.png")

        self.assertEqual("img-1", first.image_id)
        self.assertIs(first.__class__, second.__class__)
        self.assertIsInstance(dummy._media_image_uploader, MediaImageUploader)
        self.assertEqual(2, sum(1 for call in api.calls if call[0] == "prepare"))

    async def test_list_and_delete_delegate_to_api(self):
        calls = []

        class Api:
            async def get_media_images(self, *, cursor=None):
                calls.append(("list", cursor))
                return {"images": [], "total": 0}

            async def delete_media_image(self, image_id):
                calls.append(("delete", image_id))
                return {"ok": True}

        dummy = self._dummy(Api())

        page = await Client.list_media_images(dummy)
        await Client.list_media_images(dummy, cursor="next")
        await Client.delete_media_image(dummy, "img-1")

        self.assertEqual({"images": [], "total": 0}, page)
        self.assertEqual([("list", None), ("list", "next"), ("delete", "img-1")], calls)

    async def test_send_markdown_image_sends_uploaded_markdown(self):
        api = RecordingApi()
        sent = []

        async def fake_send_markdown(self, target, content, keyboard=None):
            sent.append((target, content, keyboard))
            return {"id": "msg-1"}

        dummy = self._dummy(api)
        target = ReplyTarget(scope="group", target_id="group")

        with patch.object(Client, "send_markdown", fake_send_markdown):
            result = await Client.send_markdown_image(
                dummy,
                target,
                data=PNG,
                file_name="cake.png",
                alt="蛋糕",
                keyboard={"id": "kbd"},
            )

        self.assertEqual({"id": "msg-1"}, result)
        self.assertEqual([("prepare", "put", "complete")], [tuple(call[0] for call in api.calls)])
        self.assertEqual(
            [(target, "![蛋糕 #64px #32px](qqbot-media://image/v1/token)", {"id": "kbd"})],
            sent,
        )

    async def test_send_markdown_image_propagates_upload_errors(self):
        api = RecordingApi()

        async def failing_prepare(self, **payload):
            raise RuntimeError("prepare failed")

        api.post_media_image_prepare = failing_prepare.__get__(api, RecordingApi)
        dummy = self._dummy(api)

        with self.assertRaises(RuntimeError):
            await Client.send_markdown_image(
                dummy,
                ReplyTarget(scope="c2c", target_id="user"),
                data=PNG,
                file_name="cake.png",
            )
