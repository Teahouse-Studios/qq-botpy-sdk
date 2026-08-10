import asyncio
import re
import unittest
from unittest.mock import patch

from botpy.client import Client
from botpy.middleware import (
    MentionDecision,
    RateLimitTier,
    ScopePolicy,
    SlashCommand,
    access_policy,
    concurrency_guard,
    create_middleware_context,
    mention_gate,
    rate_limiter,
    run_middleware_chain,
    slash_command,
)
from botpy.protocol import InboundMessage, ReplyTarget


def make_message(
    *,
    message_id="message-id",
    content="hello",
    scope="c2c",
    target_id="target",
    author_id="user",
    event_type=None,
    metadata=None,
):
    return InboundMessage(
        id=message_id,
        content=content,
        reply_target=ReplyTarget(scope=scope, target_id=target_id, message_id=message_id),
        event_type=event_type or ("GROUP_MESSAGE_CREATE" if scope == "group" else "C2C_MESSAGE_CREATE"),
        author_id=author_id,
        author_is_bot=False,
        metadata=metadata or {},
    )


class _Logger:
    def __init__(self):
        self.errors = []

    def error(self, message, *args):
        self.errors.append(message % args if args else message)


class _Client:
    def __init__(self, appid="12345"):
        self._appid = appid
        self.sent = []

    async def send_text(self, target, content):
        self.sent.append((target, content))
        return {"id": "sent"}


def make_context(message, client=None):
    return create_middleware_context(client or _Client(), message, _Logger())


class AccessAndMentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_access_policy_uses_scope_identifier_and_deny_first(self):
        blocked = []
        delivered = []
        middleware = access_policy(
            group=ScopePolicy(
                mode="allowlist",
                allow=(re.compile(r"^allowed"),),
                deny=("allowed-but-denied",),
            ),
            on_block=lambda context, reason: blocked.append(reason),
        )

        async def deliver(context, next_call):
            delivered.append(context.reply_target.target_id)

        allowed = make_context(make_message(scope="group", target_id="allowed-group"))
        denied = make_context(make_message(scope="group", target_id="allowed-but-denied"))
        missing = make_context(make_message(scope="group", target_id="other"))
        for context in (allowed, denied, missing):
            await run_middleware_chain((middleware, deliver), context)

        self.assertEqual(["allowed-group"], delivered)
        self.assertEqual("access:denied by deny-list (allowed-but-denied)", denied.stop_reason)
        self.assertEqual("access:not in allowlist (other)", missing.stop_reason)
        self.assertEqual(2, len(blocked))

    async def test_access_policy_supports_async_matcher(self):
        async def owner_only(context):
            await asyncio.sleep(0)
            return context.message.author_id == "owner"

        middleware = access_policy(c2c={"mode": "allowlist", "allow": [owner_only]})
        context = make_context(make_message(author_id="owner"))
        delivered = []

        async def deliver(context, next_call):
            delivered.append(True)

        await run_middleware_chain((middleware, deliver), context)
        self.assertEqual([True], delivered)

    async def test_scope_policy_accepts_single_matcher(self):
        policy = ScopePolicy(mode="allowlist", allow="owner")
        middleware = access_policy(c2c=policy)
        context = make_context(make_message(author_id="owner"))
        delivered = []

        async def deliver(context, next_call):
            delivered.append(True)

        await run_middleware_chain((middleware, deliver), context)
        self.assertEqual([True], delivered)

    async def test_mention_gate_stops_unmentioned_group_and_records_decision(self):
        skipped = []
        middleware = mention_gate(on_skip=lambda context, decision: skipped.append(decision))
        context = make_context(make_message(scope="group", content="hello"))

        await run_middleware_chain((middleware,), context)

        decision = context.state["mention"]
        self.assertIsInstance(decision, MentionDecision)
        self.assertFalse(decision.should_answer)
        self.assertEqual("mention-gate:no_mention", context.stop_reason)
        self.assertEqual([decision], skipped)

    async def test_mention_gate_accepts_event_structured_and_implicit_mentions(self):
        middleware = mention_gate(is_implicit_mention=lambda context: context.message.id == "implicit")
        messages = (
            make_message(scope="group", event_type="GROUP_AT_MESSAGE_CREATE"),
            make_message(scope="group", metadata={"mentions": [{"is_you": True}]}),
            make_message(scope="group", message_id="implicit"),
        )
        delivered = []

        async def deliver(context, next_call):
            delivered.append(context.message.id)

        for message in messages:
            await run_middleware_chain((middleware, deliver), make_context(message))
        self.assertEqual(["message-id", "message-id", "implicit"], delivered)


class RateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limiter_stops_and_reports_tier(self):
        limited = []
        middleware = rate_limiter(
            per_sender=RateLimitTier(max_requests=2, window_seconds=5),
            on_limit=lambda context, tier: limited.append(tier),
        )
        delivered = []

        async def deliver(context, next_call):
            delivered.append(context.message.id)

        with patch("botpy.middleware.control.time.monotonic", side_effect=(10.0, 11.0, 12.0, 16.0)):
            contexts = [make_context(make_message(message_id=str(index))) for index in range(4)]
            for context in contexts:
                await run_middleware_chain((middleware, deliver), context)

        self.assertEqual(["0", "1", "3"], delivered)
        self.assertEqual(["per_sender"], limited)
        self.assertEqual("rate-limit:per_sender", contexts[2].stop_reason)


class ConcurrencyGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_serializes_same_target(self):
        guard = concurrency_guard(strategy="queue", max_queue=2)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = []

        async def handle(context, next_call):
            calls.append(f"start:{context.message.id}")
            if context.message.id == "first":
                first_started.set()
                await release_first.wait()
            calls.append(f"end:{context.message.id}")

        first = asyncio.create_task(
            run_middleware_chain(
                (guard, handle),
                make_context(make_message(message_id="first", target_id="same")),
            )
        )
        await first_started.wait()
        second = asyncio.create_task(
            run_middleware_chain(
                (guard, handle),
                make_context(make_message(message_id="second", target_id="same")),
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(["start:first"], calls)

        release_first.set()
        await asyncio.gather(first, second)
        self.assertEqual(["start:first", "end:first", "start:second", "end:second"], calls)

    async def test_drop_and_queue_overflow_call_hook(self):
        dropped = []
        guard = concurrency_guard(
            strategy="queue",
            max_queue=1,
            on_drop=lambda context: dropped.append(context.message.id),
        )
        active = asyncio.Event()
        release = asyncio.Event()

        async def handle(context, next_call):
            active.set()
            if context.message.id == "active":
                await release.wait()

        active_task = asyncio.create_task(
            run_middleware_chain((guard, handle), make_context(make_message(message_id="active")))
        )
        await active.wait()
        queued = asyncio.create_task(
            run_middleware_chain((guard, handle), make_context(make_message(message_id="queued")))
        )
        await asyncio.sleep(0)
        overflow_context = make_context(make_message(message_id="overflow"))
        await run_middleware_chain((guard, handle), overflow_context)

        self.assertEqual(["overflow"], dropped)
        self.assertEqual("concurrency:queue-full", overflow_context.stop_reason)
        release.set()
        await asyncio.gather(active_task, queued)

    async def test_abort_signals_active_then_runs_new_message(self):
        guard = concurrency_guard(strategy="abort")
        first_started = asyncio.Event()
        calls = []

        async def handle(context, next_call):
            if context.message.id == "first":
                first_started.set()
                await context.abort_event.wait()
                calls.append(context.stop_reason)
            else:
                calls.append("second")

        first = asyncio.create_task(
            run_middleware_chain((guard, handle), make_context(make_message(message_id="first")))
        )
        await first_started.wait()
        second = asyncio.create_task(
            run_middleware_chain((guard, handle), make_context(make_message(message_id="second")))
        )
        await asyncio.gather(first, second)
        self.assertEqual(["concurrency:abort", "second"], calls)

    async def test_merge_combines_buffered_messages_once(self):
        guard = concurrency_guard(strategy="merge", max_queue=3)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        delivered = []

        async def handle(context, next_call):
            if context.message.id == "first":
                first_started.set()
                await release_first.wait()
            delivered.append((context.message.id, context.message.content))

        first = asyncio.create_task(
            run_middleware_chain((guard, handle), make_context(make_message(message_id="first")))
        )
        await first_started.wait()
        second = asyncio.create_task(
            run_middleware_chain(
                (guard, handle),
                make_context(make_message(message_id="second", content="two")),
            )
        )
        third = asyncio.create_task(
            run_middleware_chain(
                (guard, handle),
                make_context(make_message(message_id="third", content="three")),
            )
        )
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(first, second, third)

        self.assertEqual([("first", "hello"), ("second", "two\nthree")], delivered)

    async def test_processing_timeout_aborts_context_and_releases_target(self):
        guard = concurrency_guard(strategy="queue", max_processing_seconds=0.01)
        blocked_context = make_context(make_message(message_id="blocked"))
        delivered = []

        async def handle(context, next_call):
            if context.message.id == "blocked":
                await asyncio.Event().wait()
            delivered.append(context.message.id)

        await run_middleware_chain((guard, handle), blocked_context)
        await run_middleware_chain(
            (guard, handle),
            make_context(make_message(message_id="after-timeout")),
        )

        self.assertTrue(blocked_context.aborted)
        self.assertEqual("concurrency:processing-timeout", blocked_context.stop_reason)
        self.assertEqual(["after-timeout"], delivered)


class SlashCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_parses_arguments_replies_and_short_circuits(self):
        client = _Client()
        router = slash_command(auto_help=False)
        router.register(
            SlashCommand(
                name=("echo", "say"),
                description="echo text",
                handler=lambda context: " ".join(context.command.args),
            )
        )
        context = make_context(
            make_message(content="/say hello world", event_type="C2C_MESSAGE_CREATE"),
            client,
        )
        downstream = []

        async def deliver(context, next_call):
            downstream.append(True)

        await run_middleware_chain((router.middleware, deliver), context)

        self.assertEqual("say", context.state["command"].name)
        self.assertEqual("command:matched:say", context.stop_reason)
        self.assertEqual("hello world", client.sent[0][1])
        self.assertEqual([], downstream)

    async def test_unknown_or_disallowed_command_passes_through(self):
        router = slash_command(
            auto_help=False,
            allow_from=("owner",),
            commands=(SlashCommand(name="ping", handler=lambda context: "pong"),),
        )
        delivered = []

        async def deliver(context, next_call):
            delivered.append(context.message.content)

        for content, author_id in (("/unknown", "owner"), ("/ping", "guest")):
            context = make_context(make_message(content=content, author_id=author_id))
            await run_middleware_chain((router.middleware, deliver), context)

        self.assertEqual(["/unknown", "/ping"], delivered)

    async def test_group_command_requires_mention(self):
        client = _Client()
        router = slash_command(
            auto_help=False,
            commands=(SlashCommand(name="ping", handler=lambda context: "pong"),),
        )
        unmentioned = make_context(make_message(scope="group", content="/ping"), client)
        mentioned = make_context(
            make_message(
                scope="group",
                content="<@!12345> /ping",
                event_type="GROUP_AT_MESSAGE_CREATE",
            ),
            client,
        )
        delivered = []

        async def deliver(context, next_call):
            delivered.append(context.message.id)

        await run_middleware_chain((router.middleware, deliver), unmentioned)
        await run_middleware_chain((router.middleware, deliver), mentioned)

        self.assertEqual(["message-id"], delivered)
        self.assertEqual("pong", client.sent[0][1])


class ClientSendTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_text_routes_all_reply_target_scopes(self):
        class Api:
            def __init__(self):
                self.calls = []

            async def post_c2c_message(self, target_id, **kwargs):
                self.calls.append(("c2c", target_id, kwargs))

            async def post_group_message(self, target_id, **kwargs):
                self.calls.append(("group", target_id, kwargs))

            async def post_message(self, target_id, **kwargs):
                self.calls.append(("channel", target_id, kwargs))

            async def post_dms(self, target_id, **kwargs):
                self.calls.append(("dm", target_id, kwargs))

        dummy = type("DummyClient", (), {"api": Api()})()
        for scope in ("c2c", "group", "channel", "dm"):
            target = ReplyTarget(scope=scope, target_id=f"{scope}-id", message_id="message")
            await Client.send_text(dummy, target, "hello")

        self.assertEqual(["c2c", "group", "channel", "dm"], [call[0] for call in dummy.api.calls])
        self.assertEqual(0, dummy.api.calls[0][2]["msg_type"])
        self.assertEqual("message", dummy.api.calls[2][2]["msg_id"])


if __name__ == "__main__":
    unittest.main()
