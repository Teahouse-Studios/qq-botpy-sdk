# -*- coding: utf-8 -*-
"""出站请求整流（rate limiting）与排队行为的回归测试。"""

import asyncio
import json
import unittest

from botpy.client import Client
from botpy.flags import Intents
from botpy.http import BotHttp, Route
from botpy.protocol import ApiClient
from botpy.protocol.ratelimit import (
    DEFAULT_ROUTE_RULES,
    PROACTIVE_BOT_BUDGETS,
    MessageQuota,
    RateLimitBudget,
    RateLimiter,
    RouteRule,
    build_limiter,
    match_route,
    normalise_template,
)


class FakeClock:
    """可手动推进的单调时钟；``sleep`` 只推进时间并让出事件循环。"""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self.payload = payload
        self.headers = headers or {}

    @property
    def text(self):
        if self.payload is None:
            return ""
        return self.payload if isinstance(self.payload, str) else json.dumps(self.payload)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    @property
    def is_closed(self):
        return self.closed

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self):
        self.closed = True


class FakeTokenProvider:
    app_id = "app-id"

    async def get_access_token(self, force_refresh=False):
        return "access-token"


class RateLimitBudgetTests(unittest.TestCase):
    def test_parses_documented_limit_forms(self):
        self.assertEqual((RateLimitBudget(100, 1.0),), RateLimitBudget.parse("100 QPS"))
        self.assertEqual((RateLimitBudget(30, 60.0),), RateLimitBudget.parse("30 QPM"))
        self.assertEqual(
            (RateLimitBudget(5, 1.0), RateLimitBudget(30, 60.0)),
            RateLimitBudget.parse("5/qps & 30/qpm"),
        )
        self.assertEqual(
            (RateLimitBudget(100, 1.0),),
            RateLimitBudget.parse("100 QPS，包括主动、被动等所有消息类型"),
        )

    def test_parses_burst_capacity(self):
        (budget,) = RateLimitBudget.parse("2 QPM / 10 QPM burst")
        self.assertEqual(2, budget.limit)
        self.assertEqual(60.0, budget.window)
        self.assertEqual(10, budget.burst)
        self.assertEqual(10.0, budget.capacity)
        self.assertAlmostEqual(2 / 60, budget.rate)

    def test_rejects_invalid_budgets(self):
        for args in ((0, 1.0), (-1, 60.0), (5, 0), (5, -1)):
            with self.assertRaises(ValueError):
                RateLimitBudget(*args)
        with self.assertRaises(ValueError):
            RateLimitBudget(10, 60.0, burst=5)
        with self.assertRaises(ValueError):
            RateLimitBudget.parse("no numbers here")


class RouteMatchingTests(unittest.TestCase):
    def test_normalises_parameter_names(self):
        self.assertEqual("/v2/groups/{}/messages", normalise_template("/v2/groups/{group_openid}/messages"))
        self.assertEqual("/channels/{}", normalise_template("/channels/{channel_id}"))

    def test_matches_concrete_paths_to_templates(self):
        cases = {
            ("POST", "/v2/groups/B2C3/messages"): "POST /v2/groups/{}/messages",
            ("POST", "/v2/groups/AAA/messages"): "POST /v2/groups/{}/messages",
            ("POST", "/v2/users/ZZZ/stream_messages"): "POST /v2/users/{}/stream_messages",
            ("GET", "/gateway/bot"): "GET /gateway/bot",
            ("PUT", "/v2/panels/xyz/target"): "PUT /v2/panels/{}/target",
            ("GET", "/channels/1/messages/2/reactions/3/4"): None,
        }
        for (method, path), expected in cases.items():
            self.assertEqual(expected, match_route(method, path, DEFAULT_ROUTE_RULES), f"{method} {path}")

    def test_static_segments_win_over_parameter_segments(self):
        # /v2/groups/join_approval_strategy 不能被 /v2/groups/{}/... 抢走。
        self.assertEqual(
            "GET /v2/groups/join_approval_strategy",
            match_route("GET", "/v2/groups/join_approval_strategy", DEFAULT_ROUTE_RULES),
        )
        self.assertEqual(
            "POST /v2/groups/join_approval_strategy/{}/execute",
            match_route("POST", "/v2/groups/join_approval_strategy/77/execute", DEFAULT_ROUTE_RULES),
        )

    def test_every_documented_route_is_reachable(self):
        for key in DEFAULT_ROUTE_RULES:
            method, template = key.split(" ", 1)
            concrete = template.replace("{}", "value")
            self.assertEqual(key, match_route(method, concrete, DEFAULT_ROUTE_RULES), key)


class RateLimiterPlanTests(unittest.TestCase):
    def _limiter(self, **kwargs):
        kwargs.setdefault("clock", FakeClock())
        return RateLimiter(**kwargs)

    def _keys(self, limiter, method, path, json_body=None, route_template=None):
        plan = limiter.plan_for(method, path, json_body=json_body, route_template=route_template)
        return sorted(key for key, _budgets in plan)

    def test_passive_group_message_only_uses_route_channel(self):
        limiter = self._limiter()
        keys = self._keys(
            limiter,
            "POST",
            "/v2/groups/AAA/messages",
            json_body={"msg_type": 0, "msg_id": "ROBOT1.0_x", "content": "hi"},
        )
        self.assertEqual([("route", "POST /v2/groups/{}/messages")], keys)

    def test_proactive_group_message_adds_bot_and_relationship_channels(self):
        limiter = self._limiter()
        keys = self._keys(limiter, "POST", "/v2/groups/AAA/messages", json_body={"msg_type": 0})
        self.assertEqual(
            [
                ("bot", "proactive", "group"),
                ("proactive", "POST /v2/groups/{}/messages", "AAA"),
                ("route", "POST /v2/groups/{}/messages"),
            ],
            keys,
        )

    def test_event_id_also_marks_passive_reply(self):
        limiter = self._limiter()
        keys = self._keys(limiter, "POST", "/v2/users/U1/messages", json_body={"event_id": "evt"})
        self.assertEqual([("route", "POST /v2/users/{}/messages")], keys)

    def test_channel_always_and_proactive_budgets_use_distinct_channels(self):
        limiter = self._limiter()
        proactive = dict(limiter.plan_for("POST", "/channels/CH1/messages", json_body={"content": "hi"}))
        passive = dict(limiter.plan_for("POST", "/channels/CH1/messages", json_body={"content": "hi", "msg_id": "m"}))
        self.assertIn(("relationship", "POST /channels/{}/messages", "CH1"), proactive)
        self.assertIn(("proactive", "POST /channels/{}/messages", "CH1"), proactive)
        # 被动消息同样受「每子频道每秒 5 条」约束，但不消耗每日主动额度。
        self.assertIn(("relationship", "POST /channels/{}/messages", "CH1"), passive)
        self.assertNotIn(("proactive", "POST /channels/{}/messages", "CH1"), passive)

    def test_certification_selects_bot_dimension_budget(self):
        unverified = self._limiter(certification="unverified")
        certified = self._limiter(certification="certified")
        plan_unverified = dict(unverified.plan_for("POST", "/v2/users/U1/messages", json_body={}))
        plan_certified = dict(certified.plan_for("POST", "/v2/users/U1/messages", json_body={}))
        key = ("bot", "proactive", "c2c")
        self.assertEqual(PROACTIVE_BOT_BUDGETS["unverified"]["c2c"], plan_unverified[key])
        self.assertEqual(PROACTIVE_BOT_BUDGETS["certified"]["c2c"], plan_certified[key])

    def test_channel_rate_applies_to_passive_messages_too(self):
        limiter = self._limiter()
        keys = self._keys(
            limiter,
            "POST",
            "/channels/CH1/messages",
            json_body={"content": "hi", "msg_id": "m1"},
        )
        self.assertEqual(
            [
                ("relationship", "POST /channels/{}/messages", "CH1"),
                ("route", "POST /channels/{}/messages"),
            ],
            keys,
        )

    def test_unknown_route_falls_back_to_conservative_budget(self):
        limiter = self._limiter()
        plan = dict(limiter.plan_for("GET", "/some/brand/new/endpoint"))
        self.assertEqual((("route", "GET /some/brand/new/endpoint")), tuple(plan)[0])
        self.assertEqual(RateLimiter()._default_budgets, plan[("route", "GET /some/brand/new/endpoint")])

    def test_route_template_hint_is_honoured(self):
        limiter = self._limiter()
        plan = dict(
            limiter.plan_for(
                "POST",
                "/v2/groups/AAA/messages",
                json_body={},
                route_template="/v2/groups/{target_id}/messages",
            )
        )
        self.assertIn(("route", "POST /v2/groups/{}/messages"), plan)


class RateLimiterQueueTests(unittest.IsolatedAsyncioTestCase):
    def _limiter(self, budgets, **kwargs):
        clock = FakeClock()
        limiter = RateLimiter(
            route_rules={"POST /demo": RouteRule(tuple(budgets))},
            clock=clock,
            sleep=clock.sleep,
            **kwargs,
        )
        return limiter, clock

    async def test_sustained_rate_is_paced(self):
        limiter, clock = self._limiter([RateLimitBudget(2, 1.0)])
        stamps = []
        for _ in range(4):
            await limiter.acquire("POST", "/demo")
            stamps.append(clock.now)
        # 桶容量 2、补充速率 2/s：前两次立即放行，之后每 0.5s 一次。
        self.assertEqual([0.0, 0.0, 0.5, 1.0], stamps)

    async def test_burst_capacity_is_allowed_then_throttled(self):
        limiter, clock = self._limiter([RateLimitBudget(2, 60.0, burst=10)])
        stamps = []
        for _ in range(12):
            await limiter.acquire("POST", "/demo")
            stamps.append(round(clock.now, 6))
        self.assertEqual([0.0] * 10, stamps[:10])
        self.assertEqual(30.0, stamps[10])
        self.assertEqual(60.0, stamps[11])

    async def test_waiters_are_served_in_arrival_order(self):
        limiter, clock = self._limiter([RateLimitBudget(1, 1.0)])
        order = []

        async def worker(name):
            await limiter.acquire("POST", "/demo")
            order.append(name)

        tasks = [asyncio.create_task(worker(f"w{index}")) for index in range(5)]
        await asyncio.gather(*tasks)
        self.assertEqual(["w0", "w1", "w2", "w3", "w4"], order)
        self.assertEqual([1.0, 1.0, 1.0, 1.0], [round(value, 6) for value in clock.sleeps])

    async def test_multiple_budgets_must_all_have_tokens(self):
        limiter, clock = self._limiter([RateLimitBudget(10, 1.0), RateLimitBudget(2, 60.0)])
        stamps = []
        for _ in range(3):
            await limiter.acquire("POST", "/demo")
            stamps.append(round(clock.now, 6))
        # 1/s 的桶宽松，30s 一次的桶才是瓶颈。
        self.assertEqual([0.0, 0.0, 30.0], stamps)

    async def test_interleaved_proactive_and_passive_keep_bucket_state(self):
        limiter, _clock = self._limiter([RateLimitBudget(50, 1.0)])
        plan = dict(limiter.plan_for("POST", "/channels/CH1/messages", json_body={"msg_id": "m"}))
        self.assertEqual((RateLimitBudget(5, 1.0),), plan[("relationship", "POST /channels/{}/messages", "CH1")])
        # 主动消息会追加每日额度，但不能因为配额集合变化而重建每秒通道。
        await limiter.acquire("POST", "/channels/CH1/messages", json_body={"msg_id": "m"})
        before = limiter.snapshot()
        await limiter.acquire("POST", "/channels/CH1/messages", json_body={})
        after = limiter.snapshot()
        relationship = "relationship POST /channels/{}/messages CH1"
        entry_before = next(item for item in before["channels"] if item["key"] == relationship)
        entry_after = next(item for item in after["channels"] if item["key"] == relationship)
        # 令牌桶是同一个：4 -> 3，而不是被重置回 5。
        self.assertEqual(4.0, entry_before["available"][0])
        self.assertEqual(3.0, entry_after["available"][0])

    async def test_relationship_channels_are_isolated(self):
        limiter, clock = self._limiter(
            [RateLimitBudget(50, 1.0)],
        )
        limiter._rules["POST /v2/groups/{}/messages"] = RouteRule(
            (RateLimitBudget(50, 1.0),),
            MessageQuota(relationship_budgets=(RateLimitBudget(1, 60.0),)),
        )
        await limiter.acquire("POST", "/v2/groups/AAA/messages", json_body={})
        await limiter.acquire("POST", "/v2/groups/BBB/messages", json_body={})
        self.assertEqual(0.0, clock.now)
        await limiter.acquire("POST", "/v2/groups/AAA/messages", json_body={})
        self.assertEqual(60.0, round(clock.now, 6))

    async def test_bot_dimension_quota_is_shared_across_relationships(self):
        limiter, clock = self._limiter(
            [RateLimitBudget(50, 1.0)],
        )
        limiter._rules["POST /v2/groups/{}/messages"] = RouteRule(
            (RateLimitBudget(50, 1.0),),
            MessageQuota(bot_budgets=(RateLimitBudget(1, 60.0),)),
        )
        await limiter.acquire("POST", "/v2/groups/AAA/messages", json_body={})
        await limiter.acquire("POST", "/v2/groups/BBB/messages", json_body={})
        # Bot 维度配额是全局的，换一个群也照样受限。
        self.assertEqual(60.0, round(clock.now, 6))

    async def test_disabled_limiter_never_waits(self):
        clock = FakeClock()
        limiter = RateLimiter(enabled=False, clock=clock, sleep=clock.sleep)
        for _ in range(100):
            await limiter.acquire("POST", "/v2/groups/AAA/messages", json_body={})
        self.assertEqual([], clock.sleeps)
        self.assertEqual(0.0, clock.now)

    async def test_rate_limited_penalty_pushes_back_the_channel(self):
        limiter, clock = self._limiter([RateLimitBudget(100, 1.0)])
        await limiter.acquire("POST", "/demo")
        limiter.notify_rate_limited("POST", "/demo", retry_after=3.0)
        await limiter.acquire("POST", "/demo")
        self.assertEqual(3.0, round(clock.now, 6))

    async def test_penalty_without_retry_after_uses_default(self):
        limiter, clock = self._limiter([RateLimitBudget(100, 1.0)], default_penalty=2.5)
        limiter.notify_rate_limited("POST", "/demo")
        await limiter.acquire("POST", "/demo")
        self.assertEqual(2.5, round(clock.now, 6))

    async def test_daily_quota_does_not_block_sends(self):
        limiter, clock = self._limiter(
            [RateLimitBudget(50, 1.0)],
        )
        limiter._rules["POST /demo/{}/messages"] = RouteRule(
            (RateLimitBudget(50, 1.0),),
            MessageQuota(relationship_budgets=(RateLimitBudget(1, 86400.0),)),
        )
        with self.assertLogs("botpy.protocol.ratelimit", level="WARNING") as captured:
            await limiter.acquire("POST", "/demo/A/messages", json_body={})
            await limiter.acquire("POST", "/demo/A/messages", json_body={})
            await limiter.acquire("POST", "/demo/A/messages", json_body={})
        # 每日配额用尽只是提示，不能让消息发送挂起几小时。
        self.assertEqual(0.0, clock.now)
        self.assertEqual(1, len(captured.records))
        self.assertIn("配额已用尽", captured.records[0].getMessage())

    async def test_quota_warning_resets_after_refill(self):
        clock = FakeClock()
        limiter = RateLimiter(clock=clock, sleep=clock.sleep)
        limiter._rules["POST /demo/{}/messages"] = RouteRule(
            (RateLimitBudget(50, 1.0),),
            MessageQuota(relationship_budgets=(RateLimitBudget(1, 7200.0),)),
        )
        await limiter.acquire("POST", "/demo/A/messages", json_body={})
        with self.assertLogs("botpy.protocol.ratelimit", level="WARNING"):
            await limiter.acquire("POST", "/demo/A/messages", json_body={})
        clock.now += 7200.0
        with self.assertRaises(AssertionError):
            with self.assertLogs("botpy.protocol.ratelimit", level="WARNING"):
                await limiter.acquire("POST", "/demo/A/messages", json_body={})

    async def test_max_concurrency_limits_in_flight_requests(self):
        clock = FakeClock()
        limiter = RateLimiter(max_concurrency=2, clock=clock, sleep=clock.sleep)
        await limiter.acquire_slot()
        await limiter.acquire_slot()
        blocked = asyncio.create_task(limiter.acquire_slot())
        await asyncio.sleep(0)
        self.assertFalse(blocked.done())
        limiter.release_slot()
        await asyncio.wait_for(blocked, timeout=1)
        limiter.release_slot()
        limiter.release_slot()

    async def test_snapshot_reports_pending_waiters(self):
        limiter, clock = self._limiter([RateLimitBudget(1, 1.0)])
        await limiter.acquire("POST", "/demo")
        pending = asyncio.create_task(limiter.acquire("POST", "/demo"))
        await asyncio.sleep(0)
        snapshot = limiter.snapshot()
        self.assertTrue(snapshot["enabled"])
        entry = next(item for item in snapshot["channels"] if "POST /demo" in item["key"])
        self.assertEqual(1, entry["pending"])
        await pending
        self.assertEqual(0, limiter.snapshot()["channels"][0]["pending"])

    async def test_bucket_registry_is_bounded(self):
        clock = FakeClock()
        limiter = RateLimiter(max_buckets=8, clock=clock, sleep=clock.sleep)
        for index in range(50):
            await limiter.acquire("POST", "/v2/groups/G%d/messages" % index, json_body={})
        self.assertLessEqual(len(limiter.snapshot()["channels"]), 8)

    async def test_cancelled_waiter_releases_the_channel_lock(self):
        limiter, clock = self._limiter([RateLimitBudget(1, 1.0)])
        await limiter.acquire("POST", "/demo")
        waiter = asyncio.create_task(limiter.acquire("POST", "/demo"))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        # 锁必须已经释放，后续请求仍然可以拿到令牌。
        await asyncio.wait_for(limiter.acquire("POST", "/demo"), timeout=1)


class CertificationTierTests(unittest.TestCase):
    """默认按「已认证」档位；平台没有查询认证等级的接口，未认证需显式声明。"""

    def test_default_tier_is_certified(self):
        self.assertEqual("certified", RateLimiter().certification)
        self.assertEqual("certified", build_limiter(None).certification)

    def test_default_tier_uses_certified_budgets(self):
        plan = dict(RateLimiter().plan_for("POST", "/v2/users/U1/messages", json_body={}))
        self.assertEqual(PROACTIVE_BOT_BUDGETS["certified"]["c2c"], plan[("bot", "proactive", "c2c")])

    def test_default_tier_warns_unverified_bots_to_opt_out(self):
        with self.assertLogs("botpy.protocol.ratelimit", level="INFO") as captured:
            RateLimiter()
        message = captured.records[0].getMessage()
        self.assertIn("已认证", message)
        self.assertIn("unverified", message)
        self.assertIn("超出平台配额", message)

    def test_unverified_tier_logs_without_the_warning(self):
        with self.assertLogs("botpy.protocol.ratelimit", level="INFO") as captured:
            RateLimiter(certification="unverified")
        message = captured.records[0].getMessage()
        self.assertIn("未认证", message)
        self.assertNotIn("超出平台配额", message)

    def test_disabled_limiter_does_not_log(self):
        with self.assertRaises(AssertionError):
            with self.assertLogs("botpy.protocol.ratelimit", level="INFO"):
                RateLimiter(enabled=False)

    def test_rejects_unknown_tier(self):
        for value in ("verified", "", None, "CERTIFIED"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RateLimiter(certification=value)


class BuildLimiterTests(unittest.TestCase):
    def test_defaults_to_enabled_limiter(self):
        limiter = build_limiter(None)
        self.assertIsInstance(limiter, RateLimiter)
        self.assertTrue(limiter.enabled)

    def test_false_disables_and_instance_passes_through(self):
        self.assertIsNone(build_limiter(False))
        custom = RateLimiter(enabled=False)
        self.assertIs(build_limiter(custom), custom)

    def test_mapping_builds_configured_limiter(self):
        limiter = build_limiter({"certification": "certified", "max_concurrency": 4})
        self.assertEqual("certified", limiter.certification)
        self.assertEqual(4, limiter.max_concurrency)

    def test_rejects_unknown_types(self):
        with self.assertRaises(TypeError):
            build_limiter("nope")


class ApiClientRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_to_the_same_route_are_paced(self):
        clock = FakeClock()
        limiter = RateLimiter(
            route_rules={"GET /users/@me": RouteRule((RateLimitBudget(1, 1.0),))},
            clock=clock,
            sleep=clock.sleep,
        )
        session = FakeSession([FakeResponse(payload={"id": "1"}) for _ in range(3)])
        client = ApiClient(FakeTokenProvider(), session=session, rate_limiter=limiter)

        for _ in range(3):
            await client.request("GET", "/users/@me")

        self.assertEqual([1.0, 1.0], [round(value, 6) for value in clock.sleeps])

    async def test_retry_attempts_consume_another_token(self):
        clock = FakeClock()
        limiter = RateLimiter(
            route_rules={"GET /users/@me": RouteRule((RateLimitBudget(1, 1.0),))},
            clock=clock,
            sleep=clock.sleep,
        )
        session = FakeSession(
            [FakeResponse(status=500, payload={"message": "boom"}), FakeResponse(payload={"id": "1"})]
        )
        client = ApiClient(FakeTokenProvider(), session=session, rate_limiter=limiter, retry_base_delay=0.0)

        await client.request("GET", "/users/@me")

        self.assertEqual(2, len(session.calls))
        self.assertEqual([1.0], [round(value, 6) for value in clock.sleeps])

    async def test_429_registers_a_penalty_for_the_route(self):
        clock = FakeClock()
        limiter = RateLimiter(
            route_rules={"GET /users/@me": RouteRule((RateLimitBudget(100, 1.0),))},
            clock=clock,
            sleep=clock.sleep,
        )
        session = FakeSession([FakeResponse(status=429, payload={"message": "limited"}, headers={"Retry-After": "7"})])
        client = ApiClient(FakeTokenProvider(), session=session, rate_limiter=limiter, max_retries=0)

        with self.assertRaises(Exception):
            await client.request("GET", "/users/@me")

        # 429 携带的 Retry-After 必须落到通道上，让后续请求一起退避。
        self.assertEqual(0.0, clock.now)
        await limiter.acquire("GET", "/users/@me")
        self.assertEqual(7.0, round(clock.now, 6))

    async def test_429_retry_honours_retry_after_header(self):
        clock = FakeClock()
        limiter = RateLimiter(
            route_rules={"GET /users/@me": RouteRule((RateLimitBudget(100, 1.0),))},
            clock=clock,
            sleep=clock.sleep,
        )
        session = FakeSession(
            [
                FakeResponse(status=429, payload={"message": "limited"}, headers={"Retry-After": "7"}),
                FakeResponse(payload={"id": "1"}),
            ]
        )
        client = ApiClient(FakeTokenProvider(), session=session, rate_limiter=limiter, sleep=clock.sleep)

        await client.request("GET", "/users/@me")

        self.assertEqual(2, len(session.calls))
        self.assertEqual(7.0, round(clock.now, 6))

    async def test_non_api_origin_bypasses_the_api_queue(self):
        clock = FakeClock()
        limiter = RateLimiter(clock=clock, sleep=clock.sleep)
        session = FakeSession([FakeResponse(payload={}) for _ in range(20)])
        client = ApiClient(
            FakeTokenProvider(),
            base_url="https://api.example.test",
            session=session,
            rate_limiter=limiter,
        )

        for _ in range(20):
            await client.request("PUT", "https://cdn.example.test/upload", auth=False)

        self.assertEqual([], clock.sleeps)

    async def test_default_route_budget_applies_as_safety_net(self):
        clock = FakeClock()
        limiter = RateLimiter(
            default_budgets=(RateLimitBudget(1, 2.0),),
            clock=clock,
            sleep=clock.sleep,
        )
        session = FakeSession([FakeResponse(payload={}) for _ in range(2)])
        client = ApiClient(FakeTokenProvider(), session=session, rate_limiter=limiter)

        await client.request("GET", "/unmapped/endpoint")
        await client.request("GET", "/unmapped/endpoint")

        self.assertEqual([2.0], [round(value, 6) for value in clock.sleeps])

    async def test_limiter_is_optional(self):
        session = FakeSession([FakeResponse(payload={"ok": True})])
        client = ApiClient(FakeTokenProvider(), session=session)
        self.assertIsNone(client.rate_limiter)
        self.assertEqual({"ok": True}, await client.request("GET", "/users/@me"))


class FakeBotToken:
    """满足 :meth:`BotHttp.check_session` 需要的最小 token 替身。"""

    app_id = "app-id"

    async def check_token(self):
        return None

    def get_string(self):
        return "QQBot access-token"

    async def close(self):
        return None


class BotHttpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_http_creates_a_limiter_by_default(self):
        http = BotHttp(timeout=5, app_id="app", secret="secret")
        self.assertIsInstance(http.rate_limiter, RateLimiter)
        await http.close()

    async def test_bot_http_can_disable_rate_limiting(self):
        http = BotHttp(timeout=5, app_id="app", secret="secret", rate_limit=False)
        self.assertIsNone(http.rate_limiter)
        await http.close()

    async def test_client_exposes_limiter_and_passes_configuration(self):
        client = Client(Intents.none(), bot_log=None, rate_limit={"certification": "certified"})
        try:
            self.assertIsInstance(client.http.rate_limiter, RateLimiter)
            self.assertEqual("certified", client.http.rate_limiter.certification)
        finally:
            await client.close()
        disabled = Client(Intents.none(), bot_log=None, rate_limit=False)
        try:
            self.assertIsNone(disabled.http.rate_limiter)
        finally:
            await disabled.close()

    async def test_request_uses_the_route_template_not_the_concrete_path(self):
        http = BotHttp(timeout=5, app_id="app", secret="secret")
        session = FakeSession([FakeResponse(payload={"id": "1"})])
        http._token = FakeBotToken()
        http._client = ApiClient(
            FakeTokenProvider(),
            session=session,
            rate_limiter=http.rate_limiter,
        )
        route = Route("POST", "/v2/groups/{group_openid}/messages", group_openid="AAA")
        await http.request(route, json={"msg_type": 0, "content": "hi"})
        keys = [entry["key"] for entry in http.rate_limiter.snapshot()["channels"]]
        # 单关系通道必须按模板 + 具体 group_openid 计数，而不是按整条具体路径。
        self.assertEqual(
            [
                "bot proactive group",
                "proactive POST /v2/groups/{}/messages AAA",
                "route POST /v2/groups/{}/messages",
            ],
            sorted(keys),
        )
        await http.close()


if __name__ == "__main__":
    unittest.main()
