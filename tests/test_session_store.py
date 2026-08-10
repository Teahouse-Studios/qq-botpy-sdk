import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from botpy.client import Client
from botpy.protocol.models import SessionState
from botpy.protocol.session import JsonFileSessionStore, MemorySessionStore
from botpy.robot import Token


class MemorySessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_load_clear_and_expiry(self):
        now = [100.0]
        store = MemorySessionStore(ttl=300, clock=lambda: now[0])
        state = SessionState("session", 42, shard_id=1, shard_count=2)

        await store.save("app", state)
        self.assertEqual(state, await store.load("app", 1))

        now[0] = 401.0
        self.assertIsNone(await store.load("app", 1))

        await store.save("app", state)
        await store.clear("app", 1)
        self.assertIsNone(await store.load("app", 1))


class JsonFileSessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_across_store_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState("session", 42, shard_id=0, shard_count=2)
            first = JsonFileSessionStore(directory, save_throttle=0)
            await first.save("app", state)
            await first.close()

            second = JsonFileSessionStore(directory, save_throttle=0)
            self.assertEqual(state, await second.load("app", 0))
            await second.clear("app", 0)
            self.assertEqual([], list(Path(directory).glob("session-*.json")))

    async def test_expired_and_malformed_files_are_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100.0]
            store = JsonFileSessionStore(
                directory,
                ttl=300,
                save_throttle=0,
                clock=lambda: now[0],
            )
            await store.save("app", SessionState("session", 42, shard_id=0, shard_count=1))
            now[0] = 401.0

            self.assertIsNone(await store.load("app", 0))
            self.assertEqual([], list(Path(directory).glob("session-*.json")))

            await store.save("app", SessionState("session", 43, shard_id=0, shard_count=1))
            path = next(Path(directory).glob("session-*.json"))
            path.write_text(json.dumps({"invalid": True}), encoding="utf-8")

            self.assertIsNone(await store.load("app", 0))
            self.assertFalse(path.exists())

    async def test_throttled_save_flushes_latest_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100.0]
            store = JsonFileSessionStore(
                directory,
                save_throttle=60,
                clock=lambda: now[0],
            )
            await store.save("app", SessionState("session", 1, shard_id=0, shard_count=1))

            with patch("botpy.protocol.session.asyncio.sleep", new=AsyncMock()):
                await store.save("app", SessionState("session", 2, shard_id=0, shard_count=1))
                await store.save("app", SessionState("session", 3, shard_id=0, shard_count=1))
                await store.close()

            restored = await JsonFileSessionStore(
                directory,
                save_throttle=0,
                clock=lambda: now[0],
            ).load("app", 0)
            self.assertEqual(3, restored.sequence)


class ClientSessionRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_restores_matching_shard_session(self):
        store = MemorySessionStore()
        await store.save(
            "app",
            SessionState("saved-session", 99, shard_id=1, shard_count=2),
        )
        client = Client.__new__(Client)
        client._session_store = store
        client.intents = 1
        client._ws_ap = {"url": "wss://example.invalid"}

        session = await Client._create_gateway_session(client, Token("app", "secret"), 1, 2)

        self.assertEqual("saved-session", session["session_id"])
        self.assertEqual(99, session["last_seq"])
        self.assertIs(store, session["session_store"])

    async def test_client_discards_session_from_old_shard_layout(self):
        store = MemorySessionStore()
        await store.save(
            "app",
            SessionState("saved-session", 99, shard_id=0, shard_count=1),
        )
        client = Client.__new__(Client)
        client._session_store = store
        client.intents = 1
        client._ws_ap = {"url": "wss://example.invalid"}

        session = await Client._create_gateway_session(client, Token("app", "secret"), 0, 2)

        self.assertEqual("", session["session_id"])
        self.assertIsNone(session["last_seq"])
        self.assertIsNone(await store.load("app", 0))


if __name__ == "__main__":
    unittest.main()
