import unittest
import base64
import struct

from botpy.protocol import (
    ImageSize,
    MediaFileType,
    ReplyLimiter,
    UploadCache,
    chunk_text,
    compute_file_hash,
    detect_media_kind,
    extract_qqbot_image_size,
    format_file_size,
    format_qqbot_markdown_image,
    get_image_size_from_data_url,
    normalize_target,
    parse_data_url,
    parse_image_size,
    parse_target,
    pcm_to_wav,
    strip_amr_header,
)


class UploadCacheTests(unittest.TestCase):
    def test_cache_is_scoped_and_expires(self):
        now = [100.0]
        cache = UploadCache(clock=lambda: now[0])
        digest = compute_file_hash(b"same")

        cache.set(digest, "c2c", "user", MediaFileType.IMAGE, "info", "uuid", 120)

        self.assertEqual("info", cache.get(digest, "c2c", "user", MediaFileType.IMAGE))
        self.assertIsNone(cache.get(digest, "group", "user", MediaFileType.IMAGE))
        now[0] = 161.0
        self.assertIsNone(cache.get(digest, "c2c", "user", MediaFileType.IMAGE))

    def test_cache_is_bounded(self):
        cache = UploadCache(max_size=2, safety_margin=0)
        for index in range(3):
            cache.set(str(index), "c2c", "user", 1, f"info-{index}", "", 60)

        self.assertEqual(2, cache.stats().size)
        self.assertIsNone(cache.get("0", "c2c", "user", 1))


class ReplyLimiterTests(unittest.TestCase):
    def test_limit_and_expiry_fall_back_to_proactive(self):
        now = [0.0]
        limiter = ReplyLimiter(limit=2, ttl_seconds=10, clock=lambda: now[0])

        self.assertTrue(limiter.check("message").allowed)
        limiter.record("message")
        limiter.record("message")
        limited = limiter.check("message")
        self.assertFalse(limited.allowed)
        self.assertEqual("limit_exceeded", limited.fallback_reason)

        now[0] = 11.0
        expired = limiter.check("message")
        self.assertFalse(expired.allowed)
        self.assertEqual("expired", expired.fallback_reason)


class TextChunkTests(unittest.TestCase):
    def test_chunk_text_preserves_content(self):
        text = "x" * 10_001
        chunks = chunk_text(text)

        self.assertEqual([5000, 5000, 1], [len(chunk) for chunk in chunks])
        self.assertEqual(text, "".join(chunks))


class MediaUtilityTests(unittest.TestCase):
    def test_data_url_media_and_image_helpers(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 320, 240)
        data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

        parsed = parse_data_url(data_url)
        self.assertEqual("image/png", parsed.mime_type)
        self.assertEqual(ImageSize(320, 240), parse_image_size(parsed.data))
        self.assertEqual(ImageSize(320, 240), get_image_size_from_data_url(data_url))
        markdown = format_qqbot_markdown_image("https://example.com/a.png", ImageSize(320, 240))
        self.assertEqual(ImageSize(320, 240), extract_qqbot_image_size(markdown))
        self.assertEqual("image", detect_media_kind("https://example.com/a.png?x=1"))

    def test_target_audio_and_format_helpers(self):
        self.assertEqual("group", parse_target("qqbot:group:123").type)
        self.assertEqual("qqbot:c2c:" + "a" * 32, normalize_target("a" * 32))
        self.assertEqual("1.00 MB", format_file_size(1024 * 1024))
        self.assertEqual(b"voice", strip_amr_header(b"#!AMR\nvoice"))

        wav = pcm_to_wav(b"\0\0" * 10, 24_000)
        self.assertEqual(b"RIFF", wav[:4])
        self.assertEqual(b"WAVE", wav[8:12])


if __name__ == "__main__":
    unittest.main()
