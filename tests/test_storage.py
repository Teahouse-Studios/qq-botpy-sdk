import json
import tempfile
import unittest
from pathlib import Path

from botpy.middleware import HistoryEntry, RefEntry
from botpy.protocol import InboundAttachment, SessionState
from botpy.storage import (
    JsonFileKVStore,
    KVHistoryStore,
    KVRefIndexStore,
    KVSessionStore,
    MemoryKVStore,
)


class MemoryKVStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_ttl_prefix_keys_delete_and_clear(self):
        now = [100.0]
        store = MemoryKVStore(clock=lambda: now[0])
        await store.set("one:a", {"value": 1}, ttl=5)
        await store.set("one:b", 2)
        await store.set("two:a", 3)

        self.assertEqual({"value": 1}, await store.get("one:a"))
        self.assertEqual(["one:a", "one:b"], await store.keys("one:"))
        self.assertTrue(await store.has("one:a"))

        now[0] = 106.0
        self.assertIsNone(await store.get("one:a"))
        self.assertFalse(await store.has("one:a"))
        self.assertTrue(await store.delete("one:b"))
        await store.clear("two:")
        self.assertEqual(0, await store.size())


class JsonFileKVStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_flush_persists_latest_values_and_prefix_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonFileKVStore(directory, save_throttle=60)
            await store.set("group:a", {"value": 1})
            await store.set("group:a", {"value": 2})
            await store.set("other", "kept")
            await store.flush()

            path = Path(directory) / "kv-store.json"
            self.assertTrue(path.exists())
            self.assertEqual([], list(Path(directory).glob("*.tmp")))

            restored = JsonFileKVStore(directory)
            self.assertEqual({"value": 2}, await restored.get("group:a"))
            self.assertEqual("kept", await restored.get("other"))
            await restored.clear("group:")
            await restored.close()

            final = JsonFileKVStore(directory)
            self.assertIsNone(await final.get("group:a"))
            self.assertEqual("kept", await final.get("other"))
            await final.close()

    async def test_expired_and_corrupted_data_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100.0]
            store = JsonFileKVStore(directory, save_throttle=0, clock=lambda: now[0])
            await store.set("expiring", "value", ttl=5)
            await store.close()

            now[0] = 106.0
            restored = JsonFileKVStore(directory, clock=lambda: now[0])
            self.assertIsNone(await restored.get("expiring"))
            await restored.close()

            path = Path(directory) / "kv-store.json"
            path.write_text("{broken", encoding="utf-8")
            corrupted = JsonFileKVStore(directory)
            self.assertEqual([], await corrupted.keys())
            await corrupted.set("recovered", True)
            await corrupted.close()
            self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["entries"]["recovered"]["value"])


class KVAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_ref_and_session_restore_across_file_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonFileKVStore(directory, save_throttle=60)
            history = KVHistoryStore(store, ttl=3600)
            refs = KVRefIndexStore(store, ttl=3600)
            sessions = KVSessionStore(store, ttl=300)

            await history.append(
                "group-id",
                HistoryEntry("user", "hello", 1, "message", "User"),
                10,
            )
            await refs.set(
                "42",
                RefEntry(
                    message_id="message",
                    sender_id="user",
                    sender_name="User",
                    content="hello",
                    attachments=(
                        InboundAttachment(
                            url="https://example.invalid/image",
                            filename="image.png",
                            content_type="image/png",
                        ),
                    ),
                ),
            )
            await sessions.save(
                "app-id",
                SessionState("session", 7, shard_id=1, shard_count=2),
            )
            await sessions.close()

            restored_store = JsonFileKVStore(directory)
            restored_history = KVHistoryStore(restored_store)
            restored_refs = KVRefIndexStore(restored_store)
            restored_sessions = KVSessionStore(restored_store)

            entries = await restored_history.list("group-id", 10)
            ref = await restored_refs.get("42")
            session = await restored_sessions.load("app-id", 1)

            self.assertEqual("hello", entries[0].content)
            self.assertEqual("image.png", ref.attachments[0].filename)
            self.assertEqual("session", session.session_id)
            self.assertEqual(7, session.sequence)
            await restored_store.close()

    async def test_history_adapter_deduplicates_and_truncates(self):
        store = MemoryKVStore()
        history = KVHistoryStore(store)
        for message_id in ("one", "two", "two", "three"):
            await history.append(
                "group",
                HistoryEntry("user", message_id, 1, message_id),
                limit=2,
            )

        self.assertEqual(
            ["two", "three"],
            [entry.message_id for entry in await history.list("group", 10)],
        )
        await history.clear("group")
        self.assertEqual([], await history.list("group", 10))

    async def test_session_adapter_honors_ttl_and_rejects_invalid_state(self):
        now = [100.0]
        store = MemoryKVStore(clock=lambda: now[0])
        sessions = KVSessionStore(store, ttl=5)
        await sessions.save("app", SessionState("session", 1))
        self.assertIsNotNone(await sessions.load("app", 0))

        now[0] = 106.0
        self.assertIsNone(await sessions.load("app", 0))
        await store.set(
            sessions._key("app", 0),
            {"session_id": "session", "sequence": True, "shard_id": 0, "shard_count": 1},
        )
        self.assertIsNone(await sessions.load("app", 0))

    async def test_adapters_accept_synchronous_custom_store(self):
        class SyncStore:
            def __init__(self):
                self.values = {}

            def get(self, key):
                return self.values.get(key)

            def set(self, key, value, ttl=None):
                self.values[key] = value

            def delete(self, key):
                return self.values.pop(key, None) is not None

        store = SyncStore()
        refs = KVRefIndexStore(store)
        await refs.set("key", RefEntry("message", "user", "content"))

        self.assertEqual("content", (await refs.get("key")).content)
        self.assertTrue(await refs.delete("key"))


if __name__ == "__main__":
    unittest.main()
