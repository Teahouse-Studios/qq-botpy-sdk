import asyncio
import hashlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from botpy.api import BotAPI
from botpy.client import Client
from botpy.protocol.errors import ApiError
from botpy.protocol.media_image import (
    MediaImageCapacityPolicy,
    MediaImageQuotaExceededError,
    MediaImageQuotaPolicy,
    MediaImageRegistry,
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

    async def get_media_images(self, *, cursor=None):
        return {"images": [], "next_cursor": "", "total": 0}


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
        dummy = type("DummyClient", (), {"api": api})()
        dummy._media_image_registry = None
        dummy._media_image_registry_obj = Client._media_image_registry_obj.__get__(dummy, type(dummy))
        type(dummy).media_image_registry = Client.media_image_registry
        # 单测里不派发后台容量任务，避免事件循环关闭后残留 pending task。
        dummy._media_image_capacity_policy = MediaImageCapacityPolicy(enabled=False)
        return dummy

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

    async def test_send_markdown_image_forwards_force_verify_image_resource(self):
        api = RecordingApi()
        sent = []

        async def fake_send_markdown(self, target, content, keyboard=None, **kwargs):
            sent.append((content, keyboard, kwargs))
            return {"id": "msg-2"}

        dummy = self._dummy(api)
        target = ReplyTarget(scope="group", target_id="group")

        with patch.object(Client, "send_markdown", fake_send_markdown):
            result = await Client.send_markdown_image(
                dummy,
                target,
                data=PNG,
                file_name="cake.png",
                force_verify_image_resource=True,
            )

        self.assertEqual({"id": "msg-2"}, result)
        self.assertEqual(
            [("![cake #64px #32px](qqbot-media://image/v1/token)", None, {"force_verify_image_resource": True})],
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

    async def test_delete_media_image_updates_local_registry(self):
        api = QuotaRecordingApi(pages=[{"images": [{"image_id": "img-1"}, {"image_id": "img-2"}], "total": 2}])
        dummy = self._dummy(api)
        await Client.sync_media_images(dummy)

        await Client.delete_media_image(dummy, "img-1")

        self.assertEqual(("img-2",), dummy._media_image_registry.entries)
        self.assertEqual(["img-1"], api.deleted)

    async def test_media_image_registry_property_is_stable(self):
        dummy = self._dummy(QuotaRecordingApi())

        self.assertIs(dummy.media_image_registry, dummy.media_image_registry)

    async def test_sync_media_images_refreshes_registry(self):
        api = QuotaRecordingApi(pages=[{"images": [{"image_id": "img-9"}], "total": 1}])
        dummy = self._dummy(api)

        total = await Client.sync_media_images(dummy)

        self.assertEqual(1, total)
        self.assertEqual(("img-9",), dummy._media_image_registry.entries)


class QuotaRecordingApi(RecordingApi):
    """带素材列表与删除记录的 API 替身，用于配额释放用例。"""

    def __init__(self, pages=None, prepare_errors=None, complete_errors=None, delete_errors=None):
        super().__init__()
        self.pages = list(pages or ())
        self.prepare_errors = list(prepare_errors or ())
        self.complete_errors = list(complete_errors or ())
        self.delete_errors = dict(delete_errors or {})
        self.deleted = []

    async def post_media_image_prepare(self, **payload):
        self.calls.append(("prepare", payload))
        if self.prepare_errors:
            raise self.prepare_errors.pop(0)
        return {"upload_id": "up-1", "presigned_url": "https://cos.example.com/put"}

    async def post_media_image_complete(self, **payload):
        self.calls.append(("complete", payload))
        if self.complete_errors:
            raise self.complete_errors.pop(0)
        return {
            "image_id": "img-1",
            "url": "qqbot-media://image/v1/token",
            "ttl": 7776000,
            "preview_url": "https://preview.example.com/a.png",
        }

    async def get_media_images(self, *, cursor=None):
        self.calls.append(("list", cursor))
        if self.pages:
            return self.pages.pop(0)
        return {"images": [], "next_cursor": "", "total": 0}

    async def delete_media_image(self, image_id):
        self.calls.append(("delete", image_id))
        if image_id in self.delete_errors:
            raise self.delete_errors[image_id]
        self.deleted.append(image_id)
        return {}


def quota_error(code=40035007):
    return ApiError("quota", status=400, code=code)


class MediaImageQuotaTests(unittest.IsolatedAsyncioTestCase):
    def page(self, *ids, total=None, next_cursor=""):
        return {
            "images": [{"image_id": image_id} for image_id in ids],
            "next_cursor": next_cursor,
            "total": len(ids) if total is None else total,
        }

    async def test_quota_exceeded_deletes_oldest_page_then_retries(self):
        api = QuotaRecordingApi(
            pages=[self.page("old-1", "old-2", total=100)],
            prepare_errors=[quota_error()],
        )
        uploader = MediaImageUploader(api)

        result = await uploader.upload(data=PNG, file_name="cake.png")

        self.assertEqual(("old-2", "old-1"), result.released_images)
        self.assertEqual(["old-2", "old-1"], api.deleted)
        self.assertEqual(
            ["prepare", "list", "delete", "delete", "prepare", "put", "complete"],
            [c[0] for c in api.calls],
        )
        self.assertEqual("img-1", result.image_id)

    async def test_release_uses_the_last_page_across_pagination(self):
        api = QuotaRecordingApi(
            pages=[
                self.page("new-1", "new-2", total=104, next_cursor="c1"),
                self.page("mid-1", "mid-2", total=104, next_cursor="c2"),
                self.page("old-1", "old-2", total=104),
            ]
        )
        uploader = MediaImageUploader(api)

        release = await uploader.release_quota()

        self.assertEqual(104, release.total)
        self.assertEqual(("old-2", "old-1"), release.deleted)
        self.assertEqual([None, "c1", "c2"], [call[1] for call in api.calls if call[0] == "list"])

    async def test_protect_and_delete_per_round_limit_the_release(self):
        api = QuotaRecordingApi(pages=[self.page("a", "b", "c", "d")])
        uploader = MediaImageUploader(api)

        release = await uploader.release_quota(
            MediaImageQuotaPolicy(delete_per_round=2, protect=("a",))
        )

        self.assertEqual(("d", "c"), release.deleted)

    async def test_disabled_policy_raises_without_listing(self):
        api = QuotaRecordingApi(prepare_errors=[quota_error()])
        uploader = MediaImageUploader(api, quota_policy=MediaImageQuotaPolicy(enabled=False))

        with self.assertRaises(MediaImageQuotaExceededError):
            await uploader.upload(data=PNG, file_name="cake.png")

        self.assertEqual(["prepare"], [call[0] for call in api.calls])

    async def test_empty_oldest_page_raises_quota_error(self):
        api = QuotaRecordingApi(pages=[self.page(total=100)], prepare_errors=[quota_error()])
        uploader = MediaImageUploader(api)

        with self.assertRaises(MediaImageQuotaExceededError) as ctx:
            await uploader.upload(data=PNG, file_name="cake.png")

        self.assertEqual(100, ctx.exception.total)

    async def test_max_rounds_is_enforced(self):
        api = QuotaRecordingApi(
            pages=[self.page("old-1", total=100)] * 5,
            prepare_errors=[quota_error(), quota_error()],
        )
        uploader = MediaImageUploader(api, quota_policy=MediaImageQuotaPolicy(max_rounds=1))

        with self.assertRaises(MediaImageQuotaExceededError):
            await uploader.upload(data=PNG, file_name="cake.png")

        self.assertEqual(["old-1"], api.deleted)

    async def test_already_deleted_image_is_ignored(self):
        api = QuotaRecordingApi(
            pages=[self.page("gone", "keep", total=100)],
            prepare_errors=[quota_error()],
            delete_errors={"gone": quota_error(40035008)},
        )
        uploader = MediaImageUploader(api)

        result = await uploader.upload(data=PNG, file_name="cake.png")

        self.assertEqual(("keep",), result.released_images)

    async def test_non_quota_error_is_not_swallowed(self):
        api = QuotaRecordingApi(prepare_errors=[quota_error(40035001)])
        uploader = MediaImageUploader(api)

        with self.assertRaises(ApiError):
            await uploader.upload(data=PNG, file_name="cake.png")

    async def test_complete_quota_exceeded_releases_and_retries(self):
        api = QuotaRecordingApi(
            pages=[self.page("old-1", total=100)],
            complete_errors=[quota_error()],
        )
        uploader = MediaImageUploader(api)

        result = await uploader.upload(data=PNG, file_name="cake.png")

        self.assertEqual(("old-1",), result.released_images)
        self.assertEqual(
            ["prepare", "put", "complete", "list", "delete", "complete"],
            [call[0] for call in api.calls],
        )

    async def test_per_call_policy_overrides_instance_default(self):
        api = QuotaRecordingApi(
            pages=[self.page("old-1", "old-2", total=100)],
            prepare_errors=[quota_error()],
        )
        uploader = MediaImageUploader(api, quota_policy=MediaImageQuotaPolicy(enabled=False))

        result = await uploader.upload(
            data=PNG,
            file_name="cake.png",
            quota_policy=MediaImageQuotaPolicy(delete_per_round=1),
        )

        self.assertEqual(("old-2",), result.released_images)

    async def test_release_returns_empty_when_everything_is_protected(self):
        api = QuotaRecordingApi(pages=[self.page("a", "b")])
        uploader = MediaImageUploader(api)

        release = await uploader.release_quota(MediaImageQuotaPolicy(protect=("a", "b")))

        self.assertEqual((), release.deleted)
        self.assertEqual([], api.deleted)

    def test_capacity_policy_threshold_and_validation(self):
        policy = MediaImageCapacityPolicy()
        self.assertTrue(policy.enabled)
        self.assertEqual(0.8, policy.high_watermark)
        self.assertEqual(100, policy.max_count)
        self.assertEqual(80, policy.threshold)
        self.assertEqual(1, MediaImageCapacityPolicy(max_count=1).threshold)
        self.assertEqual(5, MediaImageCapacityPolicy(high_watermark=0.05, max_count=100).threshold)
        with self.assertRaises(ValueError):
            MediaImageCapacityPolicy(high_watermark=0)
        with self.assertRaises(ValueError):
            MediaImageCapacityPolicy(high_watermark=1.5)
        with self.assertRaises(ValueError):
            MediaImageCapacityPolicy(max_count=0)
        with self.assertRaises(TypeError):
            MediaImageCapacityPolicy(protect="img-1")


class MediaImageRegistryTests(unittest.IsolatedAsyncioTestCase):
    def page(self, *ids, total=None, next_cursor=""):
        return {
            "images": [{"image_id": image_id} for image_id in ids],
            "next_cursor": next_cursor,
            "total": len(ids) if total is None else total,
        }

    async def test_sync_keeps_newest_first_and_tracks_last_page_size(self):
        api = QuotaRecordingApi(
            pages=[
                self.page("new-1", "new-2", total=4, next_cursor="c1"),
                self.page("old-1", "old-2", total=4),
            ]
        )
        registry = MediaImageRegistry(api)

        total = await registry.sync()

        self.assertEqual(4, total)
        self.assertEqual(("new-1", "new-2", "old-1", "old-2"), registry.entries)
        self.assertEqual(2, registry.page_size)
        self.assertTrue(registry.loaded)

    async def test_record_upload_puts_new_image_at_the_head(self):
        api = QuotaRecordingApi(pages=[self.page("old-1", total=1)])
        registry = MediaImageRegistry(api)
        await registry.sync()

        registry.record_upload("fresh-1")

        self.assertEqual(("fresh-1", "old-1"), registry.entries)
        self.assertEqual(2, registry.total)

    async def test_sync_merges_uploads_that_landed_after_the_snapshot(self):
        api = QuotaRecordingApi(pages=[self.page("old-1", total=1), self.page("old-1", total=1)])
        registry = MediaImageRegistry(api)
        await registry.sync()
        registry.record_upload("fresh-1")
        registry.record_upload("fresh-2")

        await registry.sync()

        self.assertEqual(("fresh-2", "fresh-1", "old-1"), registry.entries)

    async def test_release_oldest_pops_from_the_tail(self):
        api = QuotaRecordingApi(pages=[self.page("a", "b", "c", total=3)])
        registry = MediaImageRegistry(api)

        deleted = await registry.release_oldest(count=2)

        self.assertEqual(("c", "b"), deleted)
        self.assertEqual(("a",), registry.entries)


class MediaImageCapacityTests(unittest.IsolatedAsyncioTestCase):
    def page(self, *ids, total=None):
        return {
            "images": [{"image_id": image_id} for image_id in ids],
            "next_cursor": "",
            "total": len(ids) if total is None else total,
        }

    async def test_upload_schedules_capacity_check_that_drops_the_oldest(self):
        api = QuotaRecordingApi(pages=[self.page(*[f"x{i}" for i in range(78)], "old-1", "old-2", total=80)])
        uploader = MediaImageUploader(
            api,
            capacity_policy=MediaImageCapacityPolicy(high_watermark=0.8, max_count=80),
        )

        result = await uploader.upload(data=PNG, file_name="cake.png")
        await asyncio.gather(*[task for task in uploader.registry._tasks if not task.done()])
        await asyncio.sleep(0)

        self.assertEqual("img-1", result.image_id)
        self.assertEqual(["old-2"], api.deleted)

    async def test_capacity_check_skips_when_below_the_watermark(self):
        api = QuotaRecordingApi(pages=[self.page("old-1", total=10)])
        registry = MediaImageRegistry(api)

        deleted = await registry.check_capacity(MediaImageCapacityPolicy(max_count=100))

        self.assertIsNone(deleted)
        self.assertEqual([], api.deleted)

    async def test_concurrent_checks_delete_distinct_images_in_strict_order(self):
        ids = [f"img-{index}" for index in range(120)]
        api = QuotaRecordingApi(pages=[self.page(*ids[:100], total=100)])
        registry = MediaImageRegistry(api)
        policy = MediaImageCapacityPolicy(high_watermark=0.8, max_count=100)

        results = await asyncio.gather(*[registry.check_capacity(policy) for _ in range(5)])

        self.assertEqual(5, len([item for item in results if item]))
        self.assertEqual(5, len(set(results)))
        # 删除顺序必须严格是「队尾 → 队首」，任何并发都不能重复指向同一张。
        self.assertEqual(["img-99", "img-98", "img-97", "img-96", "img-95"], api.deleted)
        self.assertEqual(set(api.deleted), set(results))
        self.assertNotIn("img-99", registry.entries)

    async def test_protected_id_is_never_deleted(self):
        api = QuotaRecordingApi(pages=[self.page("a", "b", "c", total=3)])
        registry = MediaImageRegistry(api)

        deleted = await registry.release_oldest(count=3, protect=("c",))

        self.assertEqual(("b", "a"), deleted)
        self.assertNotIn("c", api.deleted)

    async def test_sync_drops_deleted_image_from_a_stale_snapshot(self):
        """远端列表最终一致：刚删掉但快照里还在的图片不能在同步后复活。"""

        api = QuotaRecordingApi(pages=[self.page("a", "b", "c", total=3)])
        registry = MediaImageRegistry(api)
        await registry.sync()

        # 删除 c 后再同步一次，此时平台快照仍是旧的（还带着 c）。
        await registry.delete("c")
        api.pages.append(self.page("a", "b", "c", total=3))
        await registry.sync()

        self.assertEqual(("a", "b"), registry.entries)
        self.assertEqual(2, registry.total)

    async def test_capacity_check_skips_images_already_deleted_remotely(self):
        api = QuotaRecordingApi(pages=[self.page("a", "b", "c", total=3)])
        api.delete_errors["c"] = ApiError("gone", status=400, code=40035008)
        registry = MediaImageRegistry(api)

        deleted = await registry.check_capacity(MediaImageCapacityPolicy(high_watermark=0.01, max_count=3))

        self.assertEqual("b", deleted)
        self.assertEqual(("a",), registry.entries)

    async def test_synced_upload_is_not_resurrected_after_remote_removal(self):
        api = QuotaRecordingApi(
            pages=[
                self.page("old-1", total=1),
                self.page("fresh-1", "old-1", total=2),
                self.page("old-1", total=1),
            ]
        )
        registry = MediaImageRegistry(api)
        await registry.sync()
        registry.record_upload("fresh-1")

        await registry.sync()
        self.assertIn("fresh-1", registry.entries)

        # 远端确认过的上传要从待补偿队列里清掉；它被删后不能再被同步「复活」。
        await registry.sync()
        self.assertNotIn("fresh-1", registry.entries)

    def test_policy_validation(self):
        with self.assertRaises(ValueError):
            MediaImageQuotaPolicy(max_rounds=0)
        with self.assertRaises(ValueError):
            MediaImageQuotaPolicy(delete_per_round=0)
        with self.assertRaises(TypeError):
            MediaImageQuotaPolicy(protect="img-1")
        with self.assertRaises(ValueError):
            MediaImageQuotaPolicy(protect=("",))
