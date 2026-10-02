# -*- coding: utf-8 -*-
"""消息发送失败分类、回退与退避重发的回归测试。"""

import asyncio
import unittest

import httpx

from botpy.client import Client
from botpy.flags import Intents
from botpy.protocol import ApiError, MessageSendPolicy, ReplyTarget, TransportError
from botpy.protocol.errors import extract_error_code
from botpy.protocol.send_policy import (
    ACCEPTED_ERR_CODES,
    DOCUMENTED_MESSAGE_CODES,
    DUPLICATE_MESSAGE_CODES,
    PASSIVE_FALLBACK_CODES,
    TRANSIENT_CODES,
    UNREACHABLE_CODES,
    SendErrorCategory,
    classify_send_error,
    describe_send_error,
)


class FakeClock:
    """手动推进的单调时钟；``sleep`` 只推进时间，不产生真实等待。"""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def api_error(code=None, *, status=None, message="boom"):
    return ApiError(message, code=code, status=status)


def scripted(outcomes):
    """构造一个按脚本依次返回/抛错的 ``attempt``，并记录每次收到的 payload。"""

    pending = list(outcomes)
    seen = []

    async def attempt(payload):
        seen.append(dict(payload))
        if not pending:
            raise AssertionError("attempt called more times than scripted")
        item = pending.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return attempt, seen


def make_policy(**kwargs):
    clock = FakeClock()
    kwargs.setdefault("clock", clock)
    kwargs.setdefault("sleep", clock.sleep)
    return MessageSendPolicy(**kwargs), clock


class ErrorCodeExtractionTests(unittest.TestCase):
    def test_reads_platform_err_code(self):
        self.assertEqual(40034005, extract_error_code({"err_code": 40034005, "message": "x"}))

    def test_accepts_legacy_code_field(self):
        self.assertEqual(40034005, extract_error_code({"code": 40034005}))
        self.assertEqual(40034005, extract_error_code({"code": "40034005"}))

    def test_err_code_wins_over_code(self):
        self.assertEqual(7, extract_error_code({"err_code": 7, "code": 9}))

    def test_missing_or_invalid_codes(self):
        for payload in (None, {}, "text", {"err_code": None}, {"err_code": True}, {"err_code": "abc"}):
            self.assertIsNone(extract_error_code(payload), payload)

    def test_api_error_from_response_populates_code(self):
        error = ApiError.from_response(
            status=400,
            payload={"err_code": 40034005, "message": "回复消息msg_id已过期"},
            trace_id="trace",
            method="POST",
            url="https://api.bot.qq.com/x",
        )
        self.assertEqual(40034005, error.code)
        self.assertEqual("回复消息msg_id已过期", error.message)


class ClassificationTests(unittest.TestCase):
    def test_requested_codes_map_to_expected_categories(self):
        expected = {
            40034005: SendErrorCategory.PASSIVE_FALLBACK,
            40034128: SendErrorCategory.PASSIVE_FALLBACK,
            40054005: SendErrorCategory.DUPLICATE,
            40034101: SendErrorCategory.UNREACHABLE,
            40054002: SendErrorCategory.UNREACHABLE,
            40054003: SendErrorCategory.UNREACHABLE,
        }
        for code, category in expected.items():
            with self.subTest(code=code):
                self.assertEqual(category, classify_send_error(code))

    def test_uncovered_documented_codes_are_classified(self):
        # 兜底覆盖：文档错误码表里的每一条都必须落在某个分类里，绝不能是 unknown。
        unknown = sorted(code for code in DOCUMENTED_MESSAGE_CODES if classify_send_error(code) == "unknown")
        self.assertEqual([], unknown)

    def test_documented_tables_are_disjoint(self):
        tables = {
            "fallback": set(PASSIVE_FALLBACK_CODES),
            "duplicate": set(DUPLICATE_MESSAGE_CODES),
            "transient": set(TRANSIENT_CODES),
            "unreachable": set(UNREACHABLE_CODES),
        }
        names = sorted(tables)
        for index, first in enumerate(names):
            for second in names[index + 1 :]:
                self.assertEqual(set(), tables[first] & tables[second], f"{first} vs {second}")

    def test_unknown_codes_fall_back_to_unretryable(self):
        for code in (None, 0, 12345, 999999):
            self.assertEqual("unknown", classify_send_error(code))

    def test_legacy_channel_message_codes_are_covered(self):
        # 频道消息（openapi 旧接口）走的是同一套 err_code，必须同样有明确处置。
        expected = {
            304026: SendErrorCategory.PASSIVE_FALLBACK,
            304027: SendErrorCategory.PASSIVE_FALLBACK,
            304028: SendErrorCategory.PASSIVE_FALLBACK,
            304010: SendErrorCategory.TRANSIENT,
            304017: SendErrorCategory.TRANSIENT,
            304021: SendErrorCategory.TRANSIENT,
            304029: SendErrorCategory.TRANSIENT,
            304007: SendErrorCategory.TRANSIENT,
            304008: SendErrorCategory.TRANSIENT,
            304009: SendErrorCategory.TRANSIENT,
            304003: SendErrorCategory.UNREACHABLE,
            304016: SendErrorCategory.UNREACHABLE,
            304018: SendErrorCategory.UNREACHABLE,
            304025: SendErrorCategory.UNREACHABLE,
            304045: SendErrorCategory.UNREACHABLE,
            304050: SendErrorCategory.UNREACHABLE,
        }
        for code, category in expected.items():
            with self.subTest(code=code):
                self.assertEqual(category, classify_send_error(code))

    def test_async_audit_codes_are_not_classified_as_failure(self):
        # 304023/304024 是「异步受理成功」，不能出现在任何失败分类里。
        for code in sorted(ACCEPTED_ERR_CODES):
            with self.subTest(code=code):
                self.assertNotIn(code, DOCUMENTED_MESSAGE_CODES)

    def test_describe_send_error_uses_documented_wording(self):
        self.assertIn("msg_id", describe_send_error(40034005))
        self.assertIn("去重", describe_send_error(40054005))
        self.assertIn("非群成员", describe_send_error(40034101))
        self.assertEqual("", describe_send_error(999999))


class BackoffTests(unittest.TestCase):
    def test_backoff_is_base_times_power_of_two(self):
        policy, _clock = make_policy()
        self.assertEqual([3.0, 6.0, 12.0, 24.0, 48.0], [policy.backoff_delay(i) for i in range(5)])

    def test_backoff_base_is_configurable(self):
        policy, _clock = make_policy(backoff_base=1.0)
        self.assertEqual([1.0, 2.0, 4.0], [policy.backoff_delay(i) for i in range(3)])

    def test_invalid_configuration_is_rejected(self):
        for kwargs in (
            {"total_timeout": 0},
            {"total_timeout": -1},
            {"backoff_base": 0},
            {"max_attempts": 0},
            {"max_attempts": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MessageSendPolicy(**kwargs)


class PassiveFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_without_retrying(self):
        policy, clock = make_policy()
        attempt, seen = scripted([{"id": "msg-1"}])
        payload = {"content": "hi", "msg_id": "m", "msg_seq": 1}

        result = await policy.execute(payload, attempt)

        self.assertEqual({"id": "msg-1"}, result)
        self.assertEqual([], clock.sleeps)
        self.assertEqual(1, len(seen))

    async def test_every_documented_fallback_code_strips_passive_context(self):
        for code in sorted(PASSIVE_FALLBACK_CODES):
            with self.subTest(code=code):
                policy, _clock = make_policy()
                attempt, seen = scripted([api_error(code), {"id": "ok"}])
                payload = {"content": "hi", "msg_id": "m", "event_id": "e", "msg_seq": 3}

                result = await policy.execute(payload, attempt)

                self.assertEqual({"id": "ok"}, result)
                self.assertEqual(2, len(seen))
                self.assertNotIn("msg_id", seen[1])
                self.assertNotIn("event_id", seen[1])
                self.assertNotIn("msg_seq", seen[1])
                # 调用方看到的最终 payload 同样已被改写。
                self.assertNotIn("msg_id", payload)

    async def test_fallback_does_not_sleep(self):
        policy, clock = make_policy()
        attempt, _seen = scripted([api_error(40034005), {"id": "ok"}])
        await policy.execute({"content": "hi", "msg_id": "m"}, attempt)
        self.assertEqual([], clock.sleeps)

    async def test_already_proactive_message_propagates_error(self):
        policy, _clock = make_policy()
        attempt, _seen = scripted([api_error(40034005)])
        with self.assertRaises(ApiError) as caught:
            await policy.execute({"content": "hi"}, attempt)
        self.assertEqual(40034005, caught.exception.code)

    async def test_fallback_happens_at_most_once(self):
        policy, _clock = make_policy()
        attempt, seen = scripted([api_error(40034005), api_error(40034128)])
        with self.assertRaises(ApiError) as caught:
            await policy.execute({"content": "hi", "msg_id": "m"}, attempt)
        self.assertEqual(40034128, caught.exception.code)
        # 第二次已经是主动消息，没有可回退的上下文。
        self.assertEqual(2, len(seen))


class DuplicateMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_send_bumps_sequence_once_then_succeeds(self):
        policy, _clock = make_policy()
        attempt, seen = scripted([api_error(40054005), {"id": "ok"}])
        sequences = iter([7])

        result = await policy.execute(
            {"content": "hi", "msg_id": "m", "msg_seq": 1},
            attempt,
            next_sequence=lambda _payload: next(sequences),
        )

        self.assertEqual({"id": "ok"}, result)
        self.assertEqual(1, seen[0]["msg_seq"])
        self.assertEqual(7, seen[1]["msg_seq"])

    async def test_second_duplicate_raises_without_further_bump(self):
        policy, _clock = make_policy()
        attempt, seen = scripted([api_error(40054005), api_error(40054005)])
        with self.assertRaises(ApiError) as caught:
            await policy.execute(
                {"content": "hi", "msg_id": "m", "msg_seq": 1},
                attempt,
                next_sequence=lambda _payload: 2,
            )
        self.assertEqual(40054005, caught.exception.code)
        self.assertEqual(2, len(seen))

    async def test_duplicate_never_falls_back_to_proactive(self):
        policy, _clock = make_policy()
        attempt, seen = scripted([api_error(40054005), api_error(40054005)])
        with self.assertRaises(ApiError):
            await policy.execute(
                {"content": "hi", "msg_id": "m", "msg_seq": 1},
                attempt,
                next_sequence=lambda _payload: 2,
            )
        # 两次请求都保留了 msg_id：去重不做主动消息回退。
        self.assertTrue(all("msg_id" in call for call in seen))

    async def test_duplicate_without_sequence_callback_raises(self):
        policy, _clock = make_policy()
        attempt, seen = scripted([api_error(40054005)])
        with self.assertRaises(ApiError):
            await policy.execute({"content": "hi", "msg_id": "m", "msg_seq": 1}, attempt)
        self.assertEqual(1, len(seen))

    async def test_duplicate_without_msg_id_raises(self):
        policy, _clock = make_policy()
        attempt, seen = scripted([api_error(40054005)])
        with self.assertRaises(ApiError):
            await policy.execute({"content": "hi"}, attempt, next_sequence=lambda _payload: 2)
        self.assertEqual(1, len(seen))

    async def test_duplicate_during_resend_stops_and_raises(self):
        policy, clock = make_policy()
        attempt, seen = scripted([httpx.ReadTimeout("slow"), api_error(40054005)])
        with self.assertRaises(ApiError) as caught:
            await policy.execute(
                {"content": "hi", "msg_id": "m", "msg_seq": 4},
                attempt,
                next_sequence=lambda _payload: 9,
            )
        self.assertEqual(40054005, caught.exception.code)
        # 退避重发过程中被去重：不再换 seq，也不再重试。
        self.assertEqual([3.0], clock.sleeps)
        self.assertEqual(4, seen[1]["msg_seq"])


class UnreachableTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_unreachable_codes_raise_immediately(self):
        for code in sorted(UNREACHABLE_CODES):
            with self.subTest(code=code):
                policy, clock = make_policy()
                attempt, seen = scripted([api_error(code)])
                with self.assertRaises(ApiError) as caught:
                    await policy.execute({"content": "hi", "msg_id": "m"}, attempt)
                self.assertEqual(code, caught.exception.code)
                self.assertEqual(1, len(seen))
                self.assertEqual([], clock.sleeps)

    async def test_requested_unreachable_codes_are_not_retried(self):
        for code in (40034101, 40054002, 40054003):
            with self.subTest(code=code):
                policy, clock = make_policy()
                attempt, _seen = scripted([api_error(code)])
                with self.assertRaises(ApiError):
                    await policy.execute({"content": "hi"}, attempt)
                self.assertEqual([], clock.sleeps)

    async def test_unknown_code_is_not_retried(self):
        policy, clock = make_policy()
        attempt, seen = scripted([api_error(424242)])
        with self.assertRaises(ApiError):
            await policy.execute({"content": "hi"}, attempt)
        self.assertEqual(1, len(seen))
        self.assertEqual([], clock.sleeps)


class TransientRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_transient_code_retries_with_backoff(self):
        for code in sorted(TRANSIENT_CODES):
            with self.subTest(code=code):
                policy, clock = make_policy()
                attempt, seen = scripted([api_error(code), {"id": "ok"}])
                result = await policy.execute({"content": "hi"}, attempt)
                self.assertEqual({"id": "ok"}, result)
                self.assertEqual([3.0], clock.sleeps)
                self.assertEqual(2, len(seen))

    async def test_backoff_grows_exponentially(self):
        policy, clock = make_policy()
        attempt, seen = scripted([api_error(50055001)] * 4 + [{"id": "ok"}])
        result = await policy.execute({"content": "hi"}, attempt)
        self.assertEqual({"id": "ok"}, result)
        self.assertEqual([3.0, 6.0, 12.0, 24.0], clock.sleeps)
        self.assertEqual(5, len(seen))

    async def test_total_budget_stops_retrying(self):
        policy, clock = make_policy()
        attempt, seen = scripted([api_error(50055001)] * 20)
        with self.assertRaises(TransportError) as caught:
            await policy.execute({"content": "hi"}, attempt)

        error = caught.exception
        self.assertEqual(5, error.attempts)
        self.assertEqual(60.0, error.timeout)
        self.assertLessEqual(error.elapsed, 60.0)
        self.assertEqual(50055001, error.last_code)
        # 3 + 6 + 12 + 24 = 45 秒后，下一次 48 秒会越过 60 秒预算。
        self.assertEqual([3.0, 6.0, 12.0, 24.0], clock.sleeps)
        self.assertEqual(5, len(seen))

    async def test_max_attempts_cap(self):
        policy, clock = make_policy(max_attempts=2)
        attempt, seen = scripted([api_error(50055001)] * 5)
        with self.assertRaises(TransportError) as caught:
            await policy.execute({"content": "hi"}, attempt)
        self.assertEqual(2, caught.exception.attempts)
        self.assertEqual([3.0], clock.sleeps)
        self.assertEqual(2, len(seen))

    async def test_http_429_is_treated_as_transient(self):
        policy, clock = make_policy()
        attempt, _seen = scripted([api_error(None, status=429), {"id": "ok"}])
        result = await policy.execute({"content": "hi"}, attempt)
        self.assertEqual({"id": "ok"}, result)
        self.assertEqual([3.0], clock.sleeps)

    async def test_success_after_deadline_is_still_returned(self):
        policy, clock = make_policy()

        async def slow_attempt(_payload):
            # 单次尝试本身就超过了 60 秒预算，但平台已经受理，必须把结果还给调用方。
            clock.now += 100.0
            return {"id": "ok"}

        with self.assertLogs("botpy.protocol.send_policy", level="WARNING"):
            result = await policy.execute({"content": "hi"}, slow_attempt)
        self.assertEqual({"id": "ok"}, result)


class NetworkFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_passive_reply_retries_ambiguous_timeout(self):
        # 被动回复有 (msg_id, msg_seq) 去重保护，结果未知也可以安全重发。
        policy, clock = make_policy()
        attempt, _seen = scripted([httpx.ReadTimeout("slow"), {"id": "ok"}])
        self.assertEqual({"id": "ok"}, await policy.execute({"content": "hi", "msg_id": "m"}, attempt))
        self.assertEqual([3.0], clock.sleeps)

    async def test_proactive_ambiguous_timeout_is_not_replayed(self):
        # 主动消息没有去重保护，重发可能让用户收到两条，默认不重发。
        policy, clock = make_policy()
        attempt, seen = scripted([httpx.ReadTimeout("slow"), {"id": "ok"}])
        with self.assertRaises(httpx.ReadTimeout):
            await policy.execute({"content": "hi"}, attempt)
        self.assertEqual(1, len(seen))
        self.assertEqual([], clock.sleeps)

    async def test_proactive_ambiguous_timeout_replays_when_opted_in(self):
        policy, clock = make_policy(replay_ambiguous_proactive=True)
        attempt, seen = scripted([httpx.ReadTimeout("slow"), {"id": "ok"}])
        self.assertEqual({"id": "ok"}, await policy.execute({"content": "hi"}, attempt))
        self.assertEqual(2, len(seen))
        self.assertEqual([3.0], clock.sleeps)

    async def test_proactive_retries_when_request_was_never_sent(self):
        # 请求确定没发出，重发不可能重复投递，主动消息也照常重发。
        policy, clock = make_policy()
        attempt, seen = scripted([httpx.ConnectError("refused"), {"id": "ok"}])
        self.assertEqual({"id": "ok"}, await policy.execute({"content": "hi"}, attempt))
        self.assertEqual(2, len(seen))
        self.assertEqual([3.0], clock.sleeps)

    async def test_direct_httpx_connect_error_retries(self):
        policy, clock = make_policy()
        attempt, _seen = scripted([httpx.ConnectError("refused"), {"id": "ok"}])
        self.assertEqual({"id": "ok"}, await policy.execute({"content": "hi"}, attempt))
        self.assertEqual([3.0], clock.sleeps)

    async def test_invalid_url_is_not_retried(self):
        policy, clock = make_policy()
        attempt, seen = scripted([httpx.UnsupportedProtocol("bad scheme")])
        with self.assertRaises(httpx.UnsupportedProtocol):
            await policy.execute({"content": "hi"}, attempt)
        self.assertEqual([], clock.sleeps)
        self.assertEqual(1, len(seen))

    async def test_wrapped_network_failure_retries_for_passive_reply(self):
        policy, clock = make_policy()
        wrapped = TransportError("HTTP POST request failed", method="POST", cause=httpx.ReadTimeout("t"), attempts=2)
        attempt, _seen = scripted([wrapped, {"id": "ok"}])
        self.assertEqual({"id": "ok"}, await policy.execute({"content": "hi", "msg_id": "m"}, attempt))
        self.assertEqual([3.0], clock.sleeps)

    async def test_wrapped_network_failure_is_not_replayed_for_proactive(self):
        policy, clock = make_policy()
        wrapped = TransportError("HTTP POST request failed", method="POST", cause=httpx.ReadTimeout("t"), attempts=2)
        attempt, seen = scripted([wrapped, {"id": "ok"}])
        with self.assertRaises(TransportError):
            await policy.execute({"content": "hi"}, attempt)
        self.assertEqual(1, len(seen))
        self.assertEqual([], clock.sleeps)

    async def test_connect_failure_retries_even_for_proactive(self):
        policy, clock = make_policy()
        wrapped = TransportError(
            "HTTP POST request failed", method="POST", cause=httpx.ConnectError("refused"), attempts=1
        )
        attempt, _seen = scripted([wrapped, {"id": "ok"}])
        self.assertEqual({"id": "ok"}, await policy.execute({"content": "hi"}, attempt))
        self.assertEqual([3.0], clock.sleeps)

    async def test_gateway_guard_failure_is_not_retried(self):
        policy, clock = make_policy()
        guard = TransportError("Gateway 在0 秒内未恢复，消息尚未发送", method="POST", cause=asyncio.TimeoutError(), attempts=0)
        attempt, seen = scripted([guard])
        with self.assertRaises(TransportError) as caught:
            await policy.execute({"content": "hi"}, attempt)
        self.assertIs(guard, caught.exception)
        self.assertEqual([], clock.sleeps)
        self.assertEqual(1, len(seen))

    async def test_aborted_retry_is_not_retried(self):
        policy, clock = make_policy()
        inner = TransportError("Gateway 在1 秒内未恢复，消息尚未发送", method="POST", cause=asyncio.TimeoutError(), attempts=0)
        aborted = TransportError(
            "HTTP POST retry aborted before another request attempt", method="POST", cause=inner, attempts=1
        )
        attempt, seen = scripted([aborted])
        with self.assertRaises(TransportError) as caught:
            await policy.execute({"content": "hi"}, attempt)
        self.assertIs(aborted, caught.exception)
        self.assertEqual([], clock.sleeps)
        self.assertEqual(1, len(seen))

    async def test_closed_client_transport_error_is_not_retried(self):
        policy, clock = make_policy()
        error = TransportError("client is closed", method="POST", cause=None)
        attempt, _seen = scripted([error])
        with self.assertRaises(TransportError):
            await policy.execute({"content": "hi"}, attempt)
        self.assertEqual([], clock.sleeps)


class BusinessErrorInSuccessBodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonzero_err_code_in_2xx_body_is_treated_as_failure(self):
        policy, _clock = make_policy()
        attempt, seen = scripted([{"err_code": 40034005, "message": "回复消息msg_id已过期", "trace_id": "t"}, {"id": "ok"}])
        result = await policy.execute({"content": "hi", "msg_id": "m"}, attempt)
        self.assertEqual({"id": "ok"}, result)
        self.assertNotIn("msg_id", seen[1])

    async def test_zero_err_code_is_success(self):
        policy, _clock = make_policy()
        attempt, _seen = scripted([{"err_code": 0, "id": "ok"}])
        self.assertEqual({"err_code": 0, "id": "ok"}, await policy.execute({"content": "hi"}, attempt))

    async def test_absent_err_code_is_success(self):
        policy, _clock = make_policy()
        attempt, _seen = scripted([{"id": "ok", "timestamp": "2026-07-21T10:30:00+08:00"}])
        self.assertEqual(
            {"id": "ok", "timestamp": "2026-07-21T10:30:00+08:00"},
            await policy.execute({"content": "hi"}, attempt),
        )

    async def test_async_audit_codes_count_as_success(self):
        for code in sorted(ACCEPTED_ERR_CODES):
            with self.subTest(code=code):
                policy, _clock = make_policy()
                payload = {"err_code": code, "message": "等待人工审核"}
                attempt, _seen = scripted([payload])
                self.assertEqual(payload, await policy.execute({"content": "hi"}, attempt))

    async def test_business_error_in_body_retries_when_transient(self):
        policy, clock = make_policy()
        attempt, seen = scripted([{"err_code": 50055001}, {"id": "ok"}])
        self.assertEqual({"id": "ok"}, await policy.execute({"content": "hi"}, attempt))
        self.assertEqual([3.0], clock.sleeps)
        self.assertEqual(2, len(seen))

    async def test_business_error_in_body_raises_when_unreachable(self):
        policy, clock = make_policy()
        attempt, seen = scripted([{"err_code": 40034101, "message": "机器人非群成员"}])
        with self.assertRaises(ApiError) as caught:
            await policy.execute({"content": "hi"}, attempt)
        self.assertEqual(40034101, caught.exception.code)
        self.assertEqual([], clock.sleeps)
        self.assertEqual(1, len(seen))


class ScriptedApi:
    """按脚本返回响应或抛出异常的消息接口替身。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def _respond(self, kind, target_id, kwargs):
        self.calls.append((kind, target_id, kwargs))
        item = self.outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def post_group_message(self, target_id, **kwargs):
        return await self._respond("group", target_id, kwargs)

    async def post_c2c_message(self, target_id, **kwargs):
        return await self._respond("c2c", target_id, kwargs)

    async def post_message(self, target_id, **kwargs):
        return await self._respond("channel", target_id, kwargs)

    async def post_dms(self, target_id, **kwargs):
        return await self._respond("dm", target_id, kwargs)


class ClientSendIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, outcomes, **policy_kwargs):
        policy, clock = make_policy(**policy_kwargs)
        client = Client(Intents.none(), bot_log=None, send_policy=policy)
        client.api = ScriptedApi(outcomes)
        return client, clock

    async def test_expired_msg_id_falls_back_to_proactive_send(self):
        client, clock = self._client([api_error(40034005), {"id": "sent"}])
        target = ReplyTarget(scope="group", target_id="group-1", message_id="inbound-1")

        result = await Client.send(client, target, content="hi")

        self.assertEqual({"id": "sent"}, result)
        self.assertEqual(2, len(client.api.calls))
        self.assertEqual("inbound-1", client.api.calls[0][2]["msg_id"])
        self.assertNotIn("msg_id", client.api.calls[1][2])
        self.assertEqual([], clock.sleeps)
        await client.close()

    async def test_duplicate_bumps_sequence_then_succeeds(self):
        client, _clock = self._client([api_error(40054005), {"id": "sent"}])
        target = ReplyTarget(scope="c2c", target_id="user-1", message_id="inbound-2")

        await Client.send(client, target, content="hi")

        self.assertEqual(2, len(client.api.calls))
        first, second = client.api.calls[0][2], client.api.calls[1][2]
        self.assertEqual(1, first["msg_seq"])
        self.assertEqual(2, second["msg_seq"])
        self.assertEqual("inbound-2", second["msg_id"])
        await client.close()

    async def test_unreachable_code_raises_to_caller(self):
        client, _clock = self._client([api_error(40034101)])
        target = ReplyTarget(scope="group", target_id="group-1", message_id="inbound-3")

        with self.assertRaises(ApiError) as caught:
            await Client.send(client, target, content="hi")

        self.assertEqual(40034101, caught.exception.code)
        self.assertEqual(1, len(client.api.calls))
        await client.close()

    async def test_passive_reply_is_not_recorded_after_runtime_fallback(self):
        client, _clock = self._client([api_error(40034005), {"id": "sent"}])
        target = ReplyTarget(scope="group", target_id="group-1", message_id="inbound-4")

        await Client.send(client, target, content="hi")

        # 实际发出的是主动消息，不能占用被动回复次数。
        self.assertEqual(4, client._reply_limiter.check("inbound-4").remaining)
        await client.close()

    async def test_passive_reply_is_recorded_on_success(self):
        client, _clock = self._client([{"id": "sent"}])
        target = ReplyTarget(scope="group", target_id="group-1", message_id="inbound-5")

        await Client.send(client, target, content="hi")

        self.assertEqual(3, client._reply_limiter.check("inbound-5").remaining)
        await client.close()

    async def test_message_sent_hook_receives_final_payload(self):
        client, _clock = self._client([api_error(40034005), {"id": "sent", "ext_info": {"ref_idx": "REFIDX_1"}}])
        recorded = []
        client.set_message_sent_hook(lambda ref_idx, meta: recorded.append((ref_idx, meta)))
        target = ReplyTarget(scope="group", target_id="group-1", message_id="inbound-6")

        await Client.send(client, target, content="hi")

        self.assertEqual(1, len(recorded))
        self.assertEqual("REFIDX_1", recorded[0][0])
        self.assertNotIn("msg_id", recorded[0][1]["payload"])
        await client.close()

    async def test_proactive_fallback_applies_to_channel_scope(self):
        client, _clock = self._client([api_error(304103), {"id": "sent"}])
        target = ReplyTarget(scope="channel", target_id="channel-1", message_id="inbound-7")

        await Client.send(client, target, content="hi")

        self.assertEqual(2, len(client.api.calls))
        self.assertNotIn("msg_id", client.api.calls[1][2])
        await client.close()

    async def test_transient_failure_retries_within_client_send(self):
        client, clock = self._client([api_error(50055001), api_error(50055001), {"id": "sent"}])
        target = ReplyTarget(scope="c2c", target_id="user-1", message_id="inbound-8")

        await Client.send(client, target, content="hi")

        self.assertEqual(3, len(client.api.calls))
        self.assertEqual([3.0, 6.0], clock.sleeps)
        await client.close()

    async def test_send_gives_up_after_budget(self):
        client, clock = self._client([api_error(50055001)] * 20)
        target = ReplyTarget(scope="group", target_id="group-1", message_id="inbound-9")

        with self.assertRaises(TransportError) as caught:
            await Client.send(client, target, content="hi")

        self.assertEqual(5, caught.exception.attempts)
        self.assertEqual([3.0, 6.0, 12.0, 24.0], clock.sleeps)
        await client.close()

    async def test_custom_send_policy_is_honoured(self):
        client, _clock = self._client([api_error(50055001)], total_timeout=30.0, backoff_base=0.5)
        self.assertEqual(30.0, client._send_policy.total_timeout)
        self.assertEqual(0.5, client._send_policy.backoff_base)
        client.api.outcomes = [{"id": "sent"}]
        target = ReplyTarget(scope="group", target_id="group-1")
        self.assertEqual({"id": "sent"}, await Client.send(client, target, content="hi"))
        await client.close()


class SendBudgetTests(unittest.IsolatedAsyncioTestCase):
    """回退主动消息 / 换 msg_seq 这类立即重发同样受 max_attempts 与总时限约束。"""

    async def test_fallback_respects_max_attempts(self):
        policy, clock = make_policy(max_attempts=1)
        attempt, seen = scripted([api_error(40034005), {"id": "ok"}])
        with self.assertRaises(TransportError):
            await policy.execute({"content": "hi", "msg_id": "m"}, attempt, next_sequence=lambda _p: 2)
        self.assertEqual(1, len(seen))
        self.assertEqual([], clock.sleeps)

    async def test_duplicate_respects_max_attempts(self):
        policy, clock = make_policy(max_attempts=1)
        attempt, seen = scripted([api_error(40054005), {"id": "ok"}])
        with self.assertRaises(TransportError):
            await policy.execute({"content": "hi", "msg_id": "m", "msg_seq": 1}, attempt, next_sequence=lambda _p: 2)
        self.assertEqual(1, len(seen))
        self.assertEqual([], clock.sleeps)

    async def test_fallback_respects_total_deadline(self):
        policy, clock = make_policy(total_timeout=60.0)
        calls = []

        async def attempt(_payload):
            calls.append(clock.now)
            if len(calls) == 1:
                clock.now += 61.0  # 首次失败时已经超出总预算
                raise api_error(40034005)
            return {"id": "ok"}

        with self.assertRaises(TransportError) as caught:
            await policy.execute({"content": "hi", "msg_id": "m"}, attempt, next_sequence=lambda _p: 2)

        self.assertEqual(1, len(calls))
        self.assertEqual(61.0, caught.exception.elapsed)

    async def test_duplicate_respects_total_deadline(self):
        policy, clock = make_policy(total_timeout=60.0)
        calls = []

        async def attempt(_payload):
            calls.append(clock.now)
            if len(calls) == 1:
                clock.now += 61.0
                raise api_error(40054005)
            return {"id": "ok"}

        with self.assertRaises(TransportError) as caught:
            await policy.execute({"content": "hi", "msg_id": "m", "msg_seq": 1}, attempt, next_sequence=lambda _p: 2)

        self.assertEqual(1, len(calls))
        self.assertEqual(61.0, caught.exception.elapsed)

    async def test_fallback_still_works_within_budget(self):
        policy, clock = make_policy(max_attempts=3)
        attempt, seen = scripted([api_error(40034005), {"id": "ok"}])
        self.assertEqual(
            {"id": "ok"}, await policy.execute({"content": "hi", "msg_id": "m"}, attempt, next_sequence=lambda _p: 2)
        )
        self.assertEqual(2, len(seen))


if __name__ == "__main__":
    unittest.main()
