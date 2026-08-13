import asyncio
import json
import logging
import time
import unittest

import aiohttp

from botpy.errors import NotFoundError
from botpy.http import BotHttp, Route
from botpy.protocol import ApiClient, ApiError, ReplyTarget, TokenManager


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, delay=0):
        self.status = status
        self.payload = payload
        self.headers = headers or {}
        self.delay = delay

    async def text(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.payload is None:
            return ""
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps(self.payload)


class FakeRequestContext:
    def __init__(self, result):
        self.result = result

    async def __aenter__(self):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        kwargs = dict(kwargs)
        if "headers" in kwargs:
            kwargs["headers"] = dict(kwargs["headers"])
        self.calls.append(("POST", url, kwargs))
        return FakeRequestContext(self.responses.pop(0))

    def request(self, method, url, **kwargs):
        kwargs = dict(kwargs)
        if "headers" in kwargs:
            kwargs["headers"] = dict(kwargs["headers"])
        self.calls.append((method, url, kwargs))
        return FakeRequestContext(self.responses.pop(0))

    async def close(self):
        self.closed = True


class FakeTokenProvider:
    app_id = "app-id"

    def __init__(self, tokens=None):
        self.calls = 0
        self.force_refresh_calls = 0
        self.tokens = list(tokens or ["access-token"])

    async def get_access_token(self, force_refresh=False):
        self.calls += 1
        if force_refresh:
            self.force_refresh_calls += 1
        if len(self.tokens) > 1:
            return self.tokens.pop(0)
        return self.tokens[0]


class LegacyTokenProvider(FakeTokenProvider):
    async def check_token(self):
        await self.get_access_token()

    def get_string(self):
        return "QQBot access-token"

    async def close(self):
        return None


class TokenManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_token_requests_are_singleflight(self):
        session = FakeSession(
            [FakeResponse(payload={"access_token": "token", "expires_in": "7200"}, delay=0.02)]
        )
        manager = TokenManager("app", "secret", session=session)

        tokens = await asyncio.gather(*(manager.get_access_token() for _ in range(10)))

        self.assertEqual(["token"] * 10, tokens)
        self.assertEqual(1, len(session.calls))
        self.assertEqual("valid", manager.status)

    async def test_cached_token_avoids_network_request(self):
        session = FakeSession([])
        manager = TokenManager("app", "secret", session=session)
        manager.set_cached_token("cached", time.time() + 3600)

        token = await manager.get_access_token()

        self.assertEqual("cached", token)
        self.assertEqual([], session.calls)

    async def test_refresh_margin_is_fixed_when_token_is_cached(self):
        session = FakeSession([FakeResponse(payload={"access_token": "fresh", "expires_in": "7200"})])
        manager = TokenManager("app", "secret", session=session, refresh_margin=60)
        manager.set_cached_token("stale", time.time() + 30)

        token = await manager.get_access_token()

        self.assertEqual("fresh", token)
        self.assertEqual(1, len(session.calls))

    async def test_background_refresh_is_singleton_and_stops_on_close(self):
        manager = TokenManager("app", "secret", session=FakeSession([]))
        manager.set_cached_token("cached", time.time() + 3600)

        first = manager.start_background_refresh()
        second = manager.start_background_refresh()
        await asyncio.sleep(0)

        self.assertIs(first, second)
        self.assertFalse(first.done())
        await manager.close()
        self.assertTrue(first.done())


class ApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_adds_auth_headers_and_parses_json(self):
        provider = FakeTokenProvider()
        session = FakeSession([FakeResponse(payload={"ok": True})])
        client = ApiClient(provider, session=session)

        result = await client.get("/test")

        self.assertEqual({"ok": True}, result)
        method, url, kwargs = session.calls[0]
        self.assertEqual("GET", method)
        self.assertEqual("https://api.sgroup.qq.com/test", url)
        self.assertEqual("QQBot access-token", kwargs["headers"]["Authorization"])
        self.assertEqual("app-id", kwargs["headers"]["X-Union-Appid"])

    async def test_debug_log_includes_redacted_truncated_response_and_safe_url(self):
        logger = logging.getLogger("tests.botpy.protocol.response")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        session = FakeSession(
            [
                FakeResponse(
                    payload={
                        "is_end": True,
                        "access-token": "token-secret",
                        "user_openids": ["user-secret"],
                        "content": "message-secret",
                        "nested": {"safe": "x" * 2000},
                    },
                    headers={"X-Tps-trace-Id": "trace-id"},
                )
            ]
        )
        client = ApiClient(FakeTokenProvider(), session=session, logger=logger)

        with self.assertLogs(logger, level="DEBUG") as captured:
            await client.get(
                "https://upload.example.com/object?X-Amz-Signature=query-secret",
                auth=False,
            )

        output = "\n".join(captured.output)
        self.assertIn('"is_end":true', output)
        self.assertIn("<redacted>", output)
        self.assertIn("chars truncated", output)
        self.assertIn("https://upload.example.com/object", output)
        for secret in ("token-secret", "user-secret", "message-secret", "query-secret"):
            self.assertNotIn(secret, output)

    async def test_response_summary_does_not_repr_unknown_objects(self):
        from botpy.protocol.http import _summarize_payload

        class SecretObject:
            def __repr__(self):
                return "unknown-object-secret"

        summary = _summarize_payload({"value": SecretObject(), "data": b"secret bytes"})

        self.assertNotIn("unknown-object-secret", summary)
        self.assertNotIn("secret bytes", summary)
        self.assertIn("<SecretObject>", summary)
        self.assertIn("bytes", summary)

    async def test_safe_request_retries_transport_error(self):
        delays = []

        async def record_sleep(delay):
            delays.append(delay)

        provider = FakeTokenProvider()
        session = FakeSession(
            [aiohttp.ClientConnectionError("reset"), FakeResponse(payload={"ok": True})]
        )
        client = ApiClient(provider, session=session, sleep=record_sleep)

        result = await client.get("/retry")

        self.assertEqual({"ok": True}, result)
        self.assertEqual(2, len(session.calls))
        self.assertEqual([0.5], delays)

    async def test_api_error_contains_platform_context(self):
        provider = FakeTokenProvider()
        session = FakeSession(
            [
                FakeResponse(
                    status=400,
                    payload={"code": 12345, "message": "bad request"},
                    headers={"X-Tps-trace-Id": "trace-id"},
                )
            ]
        )
        client = ApiClient(provider, session=session)

        with self.assertRaises(ApiError) as caught:
            await client.post("/failed", json_body={"value": 1})

        self.assertEqual(400, caught.exception.status)
        self.assertEqual(12345, caught.exception.code)
        self.assertEqual("trace-id", caught.exception.trace_id)
        self.assertEqual("POST", caught.exception.method)

    async def test_204_returns_none(self):
        client = ApiClient(FakeTokenProvider(), session=FakeSession([FakeResponse(status=204)]))

        self.assertIsNone(await client.delete("/resource"))

    async def test_401_refreshes_token_once_and_retries(self):
        provider = FakeTokenProvider(tokens=["expired-token", "fresh-token"])
        session = FakeSession(
            [
                FakeResponse(status=401, payload={"message": "expired"}),
                FakeResponse(payload={"ok": True}),
            ]
        )
        client = ApiClient(provider, session=session, max_retries=0)

        result = await client.post("/refresh", json_body={"value": 1})

        self.assertEqual({"ok": True}, result)
        self.assertEqual(1, provider.force_refresh_calls)
        self.assertEqual("QQBot expired-token", session.calls[0][2]["headers"]["Authorization"])
        self.assertEqual("QQBot fresh-token", session.calls[1][2]["headers"]["Authorization"])

    async def test_unsafe_post_does_not_retry_by_default(self):
        provider = FakeTokenProvider()
        session = FakeSession([aiohttp.ClientConnectionError("reset")])
        client = ApiClient(provider, session=session)

        with self.assertRaises(Exception):
            await client.post("/messages", json_body={"content": "hello"})

        self.assertEqual(1, len(session.calls))


class CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_http_keeps_route_api_and_maps_legacy_error(self):
        provider = LegacyTokenProvider()
        session = FakeSession([FakeResponse(status=404, payload={"message": "missing"})])
        http = BotHttp(timeout=5)
        http._token = provider
        http._client = ApiClient(provider, session=session)

        with self.assertRaises(NotFoundError) as caught:
            await http.request(Route("GET", "/missing"))

        self.assertEqual(404, caught.exception.status)
        self.assertEqual("missing", caught.exception.message)
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertEqual("missing", str(caught.exception))

    def test_reply_target_supports_all_python_chat_scopes(self):
        for scope in ("c2c", "group", "channel", "dm"):
            target = ReplyTarget(scope=scope, target_id="target")
            self.assertEqual(scope, target.scope)

    async def test_bot_api_generic_gateway_and_token_access(self):
        class Http:
            async def request(inner_self, route, **kwargs):
                inner_self.route = route
                inner_self.kwargs = kwargs
                return {"ok": True}

            async def get_access_token(inner_self, force_refresh=False):
                inner_self.force_refresh = force_refresh
                return "token"

        from botpy.api import BotAPI

        http = Http()
        api = BotAPI(http)
        result = await api.post("/future/endpoint", {"value": 1})
        token = await api.get_token(force_refresh=True)

        self.assertEqual({"ok": True}, result)
        self.assertEqual("POST", http.route.method)
        self.assertEqual({"value": 1}, http.kwargs["json"])
        self.assertEqual("token", token)
        self.assertTrue(http.force_refresh)


if __name__ == "__main__":
    unittest.main()
