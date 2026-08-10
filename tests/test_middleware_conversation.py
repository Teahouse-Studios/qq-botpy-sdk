import asyncio
import unittest

from botpy.api import BotAPI
from botpy.client import Client
from botpy.flags import Intents
from botpy.middleware import (
    HistoryEntry,
    MemoryHistoryStore,
    MemoryRefIndexStore,
    RefEntry,
    ResolvedQuote,
    create_middleware_context,
    envelope_formatter,
    history_buffer,
    quote_ref,
    run_middleware_chain,
    typing_indicator,
)
from botpy.protocol import InboundAttachment, InboundMessage, ReplyTarget


def make_message(
    *,
    message_id="message-id",
    content="hello",
    scope="group",
    target_id="group",
    author_id="user",
    author_name="User",
    metadata=None,
    attachments=None,
):
    return InboundMessage(
        id=message_id,
        content=content,
        reply_target=ReplyTarget(scope=scope, target_id=target_id, message_id=message_id),
        event_type="GROUP_MESSAGE_CREATE" if scope == "group" else "C2C_MESSAGE_CREATE",
        author_id=author_id,
        author_name=author_name,
        author_is_bot=False,
        attachments=attachments or [],
        timestamp="2026-01-02T03:04:05+00:00",
        metadata=metadata or {},
    )


class _Logger:
    def __init__(self):
        self.errors = []
        self.debugs = []

    def error(self, message, *args):
        self.errors.append(message % args if args else message)

    def debug(self, message, *args):
        self.debugs.append(message % args if args else message)


class _Client:
    def __init__(self):
        self.typing = []

    async def send_typing(self, target, duration_seconds):
        self.typing.append((target, duration_seconds))


def make_context(message, client=None):
    return create_middleware_context(client or _Client(), message, _Logger())


class HistoryBufferTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_exposes_previous_messages_and_deduplicates(self):
        store = MemoryHistoryStore()
        middleware = history_buffer(limit=2, store=store)
        snapshots = []

        async def capture(context, next_call):
            snapshots.append([entry.message_id for entry in context.state["history"]])

        messages = (
            make_message(message_id="one", content="1"),
            make_message(message_id="two", content="2"),
            make_message(message_id="two", content="duplicate"),
            make_message(message_id="three", content="3"),
        )
        for message in messages:
            await run_middleware_chain((middleware, capture), make_context(message))

        self.assertEqual([[], ["one"], ["one", "two"], ["one", "two"]], snapshots)
        self.assertEqual(["two", "three"], [entry.message_id for entry in store.list("group", 10)])

    async def test_history_skips_non_group_messages(self):
        middleware = history_buffer()
        context = make_context(make_message(scope="c2c", target_id="user"))
        delivered = []

        async def capture(context, next_call):
            delivered.append("history" in context.state)

        await run_middleware_chain((middleware, capture), context)
        self.assertEqual([False], delivered)

    async def test_record_on_skip_controls_post_downstream_recording(self):
        async def stop(context, next_call):
            context.stop("filtered")

        skipped_store = MemoryHistoryStore()
        await run_middleware_chain(
            (history_buffer(store=skipped_store, record_on_skip=False), stop),
            make_context(make_message(message_id="skipped")),
        )
        recorded_store = MemoryHistoryStore()
        await run_middleware_chain(
            (history_buffer(store=recorded_store, record_on_skip=True), stop),
            make_context(make_message(message_id="recorded")),
        )

        self.assertEqual([], skipped_store.list("group", 10))
        self.assertEqual(["recorded"], [entry.message_id for entry in recorded_store.list("group", 10)])


class QuoteRefTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_ref_records_msg_idx_and_resolves_from_store(self):
        store = MemoryRefIndexStore(max_size=2)
        middleware = quote_ref(store=store, prefer_msg_elements=False)
        original = make_message(
            message_id="original",
            content="original content",
            metadata={"msg_idx": "42"},
        )
        reply = make_message(
            message_id="reply",
            content="reply",
            metadata={"ref_msg_idx": "42"},
        )

        await run_middleware_chain((middleware,), make_context(original))
        context = make_context(reply)
        await run_middleware_chain((middleware,), context)

        quote = context.state["quote"]
        self.assertIsInstance(quote, ResolvedQuote)
        self.assertEqual("store", quote.source)
        self.assertEqual("original content", quote.text)
        self.assertEqual("original", quote.entry.message_id)

    async def test_quote_ref_prefers_msg_elements_with_voice_text(self):
        store = MemoryRefIndexStore()
        store.set(
            "7",
            RefEntry(message_id="stored", sender_id="user", content="cached"),
        )
        metadata = {
            "ref_msg_idx": "7",
            "msg_elements": [
                {
                    "content": "pushed",
                    "attachments": [
                        {
                            "content_type": "audio/ogg",
                            "url": "https://example.invalid/voice",
                            "asr_refer_text": "voice transcript",
                        }
                    ],
                }
            ],
        }
        context = make_context(make_message(metadata=metadata))

        await run_middleware_chain((quote_ref(store=store),), context)

        quote = context.state["quote"]
        self.assertEqual("msg_elements", quote.source)
        self.assertEqual("pushed\n[voice: voice transcript]", quote.text)

    async def test_memory_ref_store_uses_lru_eviction(self):
        store = MemoryRefIndexStore(max_size=2)
        entries = [RefEntry(message_id=str(index), sender_id="user", content=str(index)) for index in range(3)]
        store.set("one", entries[0])
        store.set("two", entries[1])
        store.get("one")
        store.set("three", entries[2])

        self.assertIsNone(store.get("two"))
        self.assertIsNotNone(store.get("one"))


class EnvelopeFormatterTests(unittest.IsolatedAsyncioTestCase):
    async def test_envelope_combines_sender_quote_history_and_attachments(self):
        attachment = InboundAttachment(filename="image.png", content_type="image/png")
        context = make_context(make_message(content="current", attachments=[attachment]))
        context.state["history"] = [
            HistoryEntry("old-user", "old message", 1, "old-id", "Old User")
        ]
        context.state["quote"] = ResolvedQuote(
            ref_key="ref",
            source="store",
            entry=RefEntry("quoted", "quoted-user", "quoted text", sender_name="Quoted"),
            text="quoted text",
        )

        await run_middleware_chain((envelope_formatter(),), context)

        envelope = context.state["envelope"]
        self.assertIn("<from>", envelope)
        self.assertIn("Quoted: quoted text", envelope)
        self.assertIn("Old User: old message", envelope)
        self.assertIn("[image: image.png]", envelope)
        self.assertIn("current", envelope)

    async def test_custom_envelope_formatter_may_be_async(self):
        async def formatter(context):
            await asyncio.sleep(0)
            return f"custom:{context.message.content}"

        context = make_context(make_message())
        await run_middleware_chain((envelope_formatter(formatter=formatter),), context)
        self.assertEqual("custom:hello", context.state["envelope"])


class TypingIndicatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_typing_is_c2c_only_and_keepalive_stops(self):
        client = _Client()
        middleware = typing_indicator(
            duration_seconds=30,
            await_typing=True,
            keepalive=True,
            keepalive_interval_seconds=0.01,
        )

        async def slow_handler(context, next_call):
            await asyncio.sleep(0.025)

        c2c = make_context(make_message(scope="c2c", target_id="user"), client)
        await run_middleware_chain((middleware, slow_handler), c2c)
        calls_after_finish = len(client.typing)
        await asyncio.sleep(0.02)

        group = make_context(make_message(scope="group"), client)
        await run_middleware_chain((middleware,), group)

        self.assertGreaterEqual(calls_after_finish, 2)
        self.assertEqual(calls_after_finish, len(client.typing))
        self.assertTrue(all(duration == 30 for _, duration in client.typing))

    async def test_typing_failure_does_not_block_handler(self):
        class FailingClient:
            async def send_typing(self, target, duration_seconds):
                raise RuntimeError("typing failed")

        context = make_context(make_message(scope="c2c", target_id="user"), FailingClient())
        delivered = []

        async def handler(context, next_call):
            delivered.append(True)

        await run_middleware_chain(
            (typing_indicator(await_typing=True, keepalive=False), handler),
            context,
        )
        self.assertEqual([True], delivered)
        self.assertEqual(1, len(context.log.debugs))


class ClientContextAndTypingTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_message_context_default_preserves_on_message(self):
        calls = []

        class RecordingClient(Client):
            async def on_message(self, message):
                calls.append(message.content)

        client = RecordingClient(Intents.none(), bot_log=None)
        await client._run_message_pipeline(make_message(scope="c2c", target_id="user"))
        self.assertEqual(["hello"], calls)
        await client.close()

    async def test_on_message_context_can_read_middleware_state(self):
        calls = []

        class ContextClient(Client):
            async def on_message_context(self, context):
                calls.append(context.state["envelope"])

        client = ContextClient(
            Intents.none(),
            bot_log=None,
            middlewares=(envelope_formatter(include_sender=False),),
        )
        await client._run_message_pipeline(make_message(scope="c2c", target_id="user"))
        self.assertEqual("<message>\nhello\n</message>", calls[0])
        await client.close()

    async def test_send_text_and_typing_increment_reply_sequence(self):
        class Api:
            def __init__(self):
                self.calls = []

            async def post_c2c_message(self, target_id, **kwargs):
                self.calls.append(("text", kwargs["msg_seq"]))

            async def post_c2c_typing(self, target_id, **kwargs):
                self.calls.append(("typing", kwargs["msg_seq"]))

        dummy = type("DummyClient", (), {"api": Api()})()
        target = ReplyTarget(scope="c2c", target_id="user", message_id="same-message")

        await Client.send_text(dummy, target, "first")
        await Client.send_typing(dummy, target, 30)
        await Client.send_text(dummy, target, "second")

        self.assertEqual([("text", 1), ("typing", 2), ("text", 3)], dummy.api.calls)

    async def test_bot_api_typing_payload(self):
        class Http:
            def __init__(self):
                self.payload = None
                self.route = None

            async def request(self, route, **kwargs):
                self.route = route
                self.payload = kwargs["json"]
                return {"id": "typing"}

        http = Http()
        api = BotAPI(http)
        await api.post_c2c_typing("user", input_second=30, msg_id="message", msg_seq=7)

        self.assertEqual(6, http.payload["msg_type"])
        self.assertEqual({"input_type": 1, "input_second": 30}, http.payload["input_notify"])
        self.assertEqual("message", http.payload["msg_id"])
        self.assertEqual(7, http.payload["msg_seq"])


if __name__ == "__main__":
    unittest.main()
