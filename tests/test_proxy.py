import asyncio
import importlib.util
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from botpy.client import Client
from botpy.flags import Intents
from botpy.gateway import BotWebSocket
from botpy.http import BotHttp
from botpy.protocol import ApiClient, TokenManager, describe_proxy, normalize_proxy
from botpy.robot import Token


class FakeTokenProvider:
    def __init__(self, token="access-token"):
        self.app_id = "app-id"
        self.token = token

    async def get_access_token(self, force_refresh=False):
        return self.token


class DummyToken:
    app_id = "app-id"

    def __init__(self):
        self.access_token = "token"

    async def get_access_token(self):
        return self.access_token

    def get_string(self, token=None):
        return "QQBot %s" % (token or self.access_token)


class DummyConnection:
    def __init__(self, loop):
        self.loop = loop
        self.parser = {}
        self.sessions = []

    def add(self, session, *, is_reconnect=False):
        self.sessions.append(session)

    def claim_gateway(self, session, owner):
        return True

    def mark_ready(self, session, *, owner=None):
        return True

    def mark_disconnected(self, session, *, owner=None):
        return True

    def is_gateway_owner(self, session, owner):
        return True


class RecordingAsyncClient:
    """记录 httpx.AsyncClient 构造参数的替身。"""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        RecordingAsyncClient.instances.append(self)

    @property
    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def aclose(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.closed = True
        return False


class ProxyConfigTests(unittest.TestCase):
    def setUp(self):
        RecordingAsyncClient.instances.clear()

    def test_none_and_blank_strings_disable_proxy(self):
        self.assertIsNone(normalize_proxy(None))
        self.assertIsNone(normalize_proxy(""))
        self.assertIsNone(normalize_proxy("   "))

    def test_string_proxy_is_normalized(self):
        proxy = normalize_proxy("http://127.0.0.1:3128")

        self.assertIsInstance(proxy, httpx.Proxy)
        self.assertEqual("http", proxy.url.scheme)
        self.assertEqual("127.0.0.1", proxy.url.host)
        self.assertEqual(3128, proxy.url.port)

    def test_proxy_credentials_are_kept_but_hidden_in_logs(self):
        proxy = normalize_proxy("http://user:s3cret@proxy.local:8080")

        self.assertEqual(("user", "s3cret"), proxy.auth)
        description = describe_proxy(proxy)
        self.assertEqual("http://user:***@proxy.local:8080", description)
        self.assertNotIn("s3cret", description)

    def test_existing_httpx_proxy_is_reused(self):
        proxy = httpx.Proxy("https://proxy.local:8443")

        self.assertIs(proxy, normalize_proxy(proxy))

    def test_httpx_url_is_accepted(self):
        proxy = normalize_proxy(httpx.URL("http://proxy.local:3128"))

        self.assertIsInstance(proxy, httpx.Proxy)
        self.assertEqual(3128, proxy.url.port)

    def test_invalid_proxy_configs_are_rejected(self):
        for value in (
            "ftp://proxy.local:3128",
            "proxy.local:3128",
            "http://",
            "http://proxy.local:99999",
            "http://pro xy.local:3128",
            "http://pro\nxy.local:3128",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_proxy(value)

    def test_surrounding_whitespace_is_trimmed(self):
        proxy = normalize_proxy("  http://127.0.0.1:3128\n")

        self.assertEqual(3128, proxy.url.port)

    def test_unsupported_proxy_types_are_rejected(self):
        for value in (123, object(), b"http://proxy.local:3128", ["http://proxy.local:3128"]):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    normalize_proxy(value)

    @unittest.skipIf(importlib.util.find_spec("socksio") is not None, "socksio is installed")
    def test_socks_proxy_requires_socksio(self):
        with self.assertRaises(ValueError) as caught:
            normalize_proxy("socks5://127.0.0.1:1080")

        self.assertIn("socksio", str(caught.exception))

    def test_describe_proxy_tolerates_raw_and_invalid_values(self):
        self.assertEqual("<none>", describe_proxy(None))
        self.assertEqual("http://127.0.0.1:3128", describe_proxy("http://127.0.0.1:3128"))
        self.assertEqual("<invalid proxy>", describe_proxy("ftp://proxy.local:3128"))


class ApiClientProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        RecordingAsyncClient.instances.clear()

    async def test_created_session_receives_proxy_and_verify(self):
        client = ApiClient(FakeTokenProvider(), ssl=False, proxy="http://127.0.0.1:3128")

        with patch.object(httpx, "AsyncClient", RecordingAsyncClient):
            session = await client._get_session()

        self.assertIs(session, RecordingAsyncClient.instances[0])
        self.assertEqual("http://127.0.0.1:3128", str(session.kwargs["proxy"].url))
        self.assertFalse(session.kwargs["verify"])
        await client.close()

    async def test_created_session_without_proxy_passes_none(self):
        client = ApiClient(FakeTokenProvider())

        with patch.object(httpx, "AsyncClient", RecordingAsyncClient):
            session = await client._get_session()

        self.assertIsNone(session.kwargs["proxy"])
        self.assertTrue(session.kwargs["verify"])
        await client.close()

    async def test_invalid_proxy_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            ApiClient(FakeTokenProvider(), proxy="ftp://proxy.local:3128")


class TokenManagerProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        RecordingAsyncClient.instances.clear()

    async def test_created_session_receives_proxy_and_verify(self):
        manager = TokenManager("app-id", "secret", ssl=False, proxy="http://user:pass@proxy.local:8080")

        with patch.object(httpx, "AsyncClient", RecordingAsyncClient):
            session = await manager._get_session()

        self.assertIs(session, RecordingAsyncClient.instances[0])
        self.assertEqual("http://proxy.local:8080", str(session.kwargs["proxy"].url))
        self.assertEqual(("user", "pass"), session.kwargs["proxy"].auth)
        self.assertFalse(session.kwargs["verify"])
        await manager.close()

    async def test_created_session_without_proxy_passes_none(self):
        manager = TokenManager("app-id", "secret")

        with patch.object(httpx, "AsyncClient", RecordingAsyncClient):
            session = await manager._get_session()

        self.assertIsNone(session.kwargs["proxy"])
        await manager.close()


class TransportPlumbingTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_http_hands_proxy_to_token_and_api_client(self):
        recorded = {}

        class RecordingApiClient:
            def __init__(self, token, **kwargs):
                recorded["token"] = token
                recorded.update(kwargs)

        http = BotHttp(timeout=5, app_id="app-id", secret="secret", proxy="http://127.0.0.1:3128")
        http._token.check_token = AsyncMock()

        with patch("botpy.http.ApiClient", RecordingApiClient):
            await http.check_session()

        self.assertIs(recorded["token"], http._token)
        self.assertIs(recorded["proxy"], http.proxy)
        self.assertEqual("http://127.0.0.1:3128", describe_proxy(http.proxy))
        self.assertEqual("http://127.0.0.1:3128", describe_proxy(http._token.proxy))

    async def test_token_forwards_proxy_to_manager(self):
        token = Token("app-id", "secret", proxy="http://127.0.0.1:3128")

        self.assertEqual("http://127.0.0.1:3128", describe_proxy(token.proxy))
        self.assertIs(token.proxy, token._manager.proxy)

    async def test_client_forwards_proxy_to_http_token_and_gateway_session(self):
        client = Client(Intents.none(), bot_log=None, proxy="http://user:secret@127.0.0.1:3128")
        try:
            self.assertEqual("http://user:***@127.0.0.1:3128", describe_proxy(client.http.proxy))
            self.assertEqual(("user", "secret"), client.http.proxy.auth)

            token = Token("app-id", "secret", proxy=client._proxy)
            client._ws_ap = {"url": "wss://gateway.example.invalid"}
            session = await client._create_gateway_session(token, 0, 1)

            self.assertIs(client.http.proxy, session["proxy"])
            self.assertIs(token.proxy, session["proxy"])
        finally:
            await client.http.close()

    async def test_client_rejects_invalid_proxy(self):
        with self.assertRaises(ValueError):
            Client(Intents.none(), bot_log=None, proxy="ftp://proxy.local:3128")


class GatewayProxyTests(unittest.IsolatedAsyncioTestCase):
    class FakeSocket:
        closed = False

        def __init__(self):
            self.close_code = None

        async def receive(self):
            # 心跳 ACK 会被 _is_system_event 消费，随后 closed 让接收循环退出。
            self.closed = True
            return json.dumps({"op": 11})

    async def asyncSetUp(self):
        RecordingAsyncClient.instances.clear()
        self.connection = DummyConnection(asyncio.get_running_loop())
        self.session = {
            "session_id": "session-id",
            "last_seq": None,
            "intent": 1,
            "token": DummyToken(),
            "url": "wss://gateway.example.invalid",
            "shards": {"shard_id": 0, "shard_count": 1},
        }
        self.socket = GatewayProxyTests.FakeSocket()

    async def test_ws_connect_uses_configured_proxy(self):
        proxy = normalize_proxy("http://127.0.0.1:3128")
        self.session["proxy"] = proxy
        self.session["ssl"] = False

        def fake_aconnect_ws(url, session):
            self.recorded_url = url
            return _AsyncContext(self.socket)

        gateway = BotWebSocket(self.session, self.connection)
        with patch.object(httpx, "AsyncClient", RecordingAsyncClient):
            with patch("botpy.gateway.aconnect_ws", fake_aconnect_ws):
                await gateway.ws_connect()

        self.assertEqual("wss://gateway.example.invalid", self.recorded_url)
        self.assertIs(proxy, RecordingAsyncClient.instances[0].kwargs["proxy"])
        self.assertFalse(RecordingAsyncClient.instances[0].kwargs["verify"])

    async def test_ws_connect_without_proxy_passes_none(self):
        def fake_aconnect_ws(url, session):
            return _AsyncContext(self.socket)

        gateway = BotWebSocket(self.session, self.connection)
        with patch.object(httpx, "AsyncClient", RecordingAsyncClient):
            with patch("botpy.gateway.aconnect_ws", fake_aconnect_ws):
                await gateway.ws_connect()

        self.assertIsNone(RecordingAsyncClient.instances[0].kwargs["proxy"])


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False


class ProxyEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """用一个真实的本地回环代理验证请求确实经由代理转发。"""

    async def _start_proxy(self, payload=None):
        received = []

        async def handle(reader, writer):
            try:
                request_line = await reader.readline()
                received.append(request_line)
                while True:
                    line = await reader.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break
                body = json.dumps(payload if payload is not None else {"ok": True}).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body
                )
                await writer.drain()
            finally:
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        return server, port, received

    async def test_http_request_is_sent_to_the_configured_proxy(self):
        server, port, received = await self._start_proxy()
        client = ApiClient(
            FakeTokenProvider(),
            base_url="http://api.example.invalid",
            proxy=f"http://127.0.0.1:{port}",
        )
        try:
            result = await client.get("/test")
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

        self.assertEqual({"ok": True}, result)
        self.assertTrue(received, "代理没有收到任何请求")
        # 明文 HTTP 走代理时使用 absolute-form 请求行，且不解析目标域名。
        self.assertIn(b"GET http://api.example.invalid/test HTTP/1.1", received[0])

    async def test_access_token_request_is_sent_to_the_configured_proxy(self):
        server, port, received = await self._start_proxy(
            payload={"access_token": "token-from-proxy", "expires_in": 7200}
        )
        manager = TokenManager(
            "app-id",
            "secret",
            base_url="http://api.example.invalid",
            proxy=f"http://127.0.0.1:{port}",
        )
        try:
            token = await manager.get_access_token()
        finally:
            await manager.close()
            server.close()
            await server.wait_closed()

        self.assertEqual("token-from-proxy", token)
        self.assertTrue(received, "代理没有收到任何请求")
        self.assertIn(b"POST http://api.example.invalid/app/getAppAccessToken HTTP/1.1", received[0])


if __name__ == "__main__":
    unittest.main()
