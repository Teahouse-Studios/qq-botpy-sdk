import asyncio
from dataclasses import replace
import unittest
from unittest.mock import patch

from botpy.client import Client
from botpy.flags import Intents
from botpy.middleware import (
    MiddlewareContext,
    content_sanitizer,
    create_middleware_context,
    error_handler,
    message_filter,
    resolve_policy,
    run_middleware_chain,
)
from botpy.protocol import InboundMessage, ReplyTarget


def make_message(**changes):
    message = InboundMessage(
        id="message-id",
        content="hello",
        reply_target=ReplyTarget(scope="c2c", target_id="user", message_id="message-id"),
        event_type="C2C_MESSAGE_CREATE",
        author_id="user",
        author_is_bot=False,
    )
    return replace(message, **changes)


class MiddlewareCoreTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self, message=None):
        return MiddlewareContext(object(), message or make_message(), _RecordingLogger())

    async def test_middleware_wraps_downstream_in_order(self):
        calls = []

        async def first(context, next_call):
            calls.append("first-before")
            context.state["value"] = 42
            await next_call()
            calls.append("first-after")

        async def second(context, next_call):
            calls.append(context.state["value"])
            await next_call()

        completed = await run_middleware_chain((first, second), self.make_context())

        self.assertTrue(completed)
        self.assertEqual(["first-before", 42, "first-after"], calls)

    async def test_stop_and_abort_prevent_downstream(self):
        calls = []
        context = self.make_context()

        async def stop(context, next_call):
            context.abort("replaced")

        async def downstream(context, next_call):
            calls.append("unexpected")

        completed = await run_middleware_chain((stop, downstream), context)

        self.assertFalse(completed)
        self.assertTrue(context.aborted)
        self.assertTrue(context.abort_event.is_set())
        self.assertEqual("replaced", context.stop_reason)
        self.assertEqual([], calls)

    async def test_next_cannot_be_called_twice(self):
        async def invalid(context, next_call):
            await next_call()
            await next_call()

        with self.assertRaisesRegex(RuntimeError, "multiple times"):
            await run_middleware_chain((invalid,), self.make_context())

    async def test_resolve_policy_priority(self):
        context = self.make_context()
        context.state["policy"] = {"group": {"limit": 3}}

        self.assertEqual(5, resolve_policy(context, "group.limit", 5, 1))
        self.assertEqual(3, resolve_policy(context, "group.limit", None, 1))
        self.assertEqual(1, resolve_policy(context, "group.missing", None, 1))


class BuiltinMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_filter_drops_self_echo_and_duplicates(self):
        middleware = message_filter(window_seconds=5)
        delivered = []

        async def deliver(context, next_call):
            delivered.append(context.message.id)

        self_echo = create_middleware_context(
            object(), make_message(author_is_bot=True), _RecordingLogger()
        )
        await run_middleware_chain((middleware, deliver), self_echo)
        self.assertEqual("self-echo", self_echo.stop_reason)

        first = create_middleware_context(object(), make_message(), _RecordingLogger())
        duplicate = create_middleware_context(object(), make_message(), _RecordingLogger())
        with patch("botpy.middleware.builtin.time.monotonic", side_effect=(10.0, 11.0)):
            await run_middleware_chain((middleware, deliver), first)
            await run_middleware_chain((middleware, deliver), duplicate)

        self.assertEqual(["message-id"], delivered)
        self.assertEqual("deduplication", duplicate.stop_reason)

    async def test_message_filter_allows_id_after_window(self):
        middleware = message_filter(window_seconds=5)
        delivered = []

        async def deliver(context, next_call):
            delivered.append(context.message.id)

        with patch("botpy.middleware.builtin.time.monotonic", side_effect=(10.0, 16.0)):
            for _ in range(2):
                context = create_middleware_context(object(), make_message(), _RecordingLogger())
                await run_middleware_chain((middleware, deliver), context)

        self.assertEqual(["message-id", "message-id"], delivered)

    async def test_content_sanitizer_rewrites_frozen_message(self):
        client = type("ClientStub", (), {"_appid": "12345"})()
        message = make_message(content="  <@!12345> hello   [<face,id=16/>]  ")
        context = create_middleware_context(client, message, _RecordingLogger())

        await run_middleware_chain(
            (content_sanitizer(collapse_whitespace=True, parse_face_tags=True),),
            context,
        )

        self.assertEqual("hello 👍", context.message.content)
        self.assertIsNot(message, context.message)
        self.assertEqual(message.reply_target, context.reply_target)

    async def test_error_handler_can_consume_or_rethrow(self):
        handled = []

        async def fail(context, next_call):
            raise ValueError("broken")

        async def handle(error, context):
            handled.append(str(error))

        context = create_middleware_context(object(), make_message(), _RecordingLogger())
        await run_middleware_chain((error_handler(handle), fail), context)
        self.assertEqual(["broken"], handled)

        context = create_middleware_context(object(), make_message(), _RecordingLogger())
        with self.assertRaisesRegex(ValueError, "broken"):
            await run_middleware_chain((error_handler(handle, rethrow=True), fail), context)


class ClientMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_runs_middleware_before_on_message(self):
        calls = []

        class RecordingClient(Client):
            async def on_message(self, message):
                calls.append(("handler", message.content))

        async def annotate(context, next_call):
            calls.append(("middleware", context.message.content))
            context.message = replace(context.message, content="changed")
            await next_call()

        client = RecordingClient(Intents.none(), bot_log=None, middlewares=(annotate,))
        await client._run_message_pipeline(make_message())

        self.assertEqual([("middleware", "hello"), ("handler", "changed")], calls)
        self.assertIs(client, client.use())
        await client.close()

    async def test_client_use_rejects_non_callable_and_closed_client(self):
        client = Client(Intents.none(), bot_log=None)
        with self.assertRaises(TypeError):
            client.use(None)

        await client.close()
        with self.assertRaises(RuntimeError):
            client.use(lambda context, next_call: asyncio.sleep(0))


class _RecordingLogger:
    def __init__(self):
        self.errors = []

    def error(self, message, *args):
        self.errors.append(message % args if args else message)


if __name__ == "__main__":
    unittest.main()
