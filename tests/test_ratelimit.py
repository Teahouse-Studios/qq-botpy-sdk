# -*- coding: utf-8 -*-
"""出站请求整流（rate limiting）与排队行为的回归测试。"""

import asyncio
import json
import unittest

from botpy.api import BotAPI
from botpy.client import Client
from botpy.flags import Intents
from botpy.http import BotHttp, Route
from botpy.protocol import ApiClient, ReplyTarget
from botpy.protocol.ratelimit import (
    DEFAULT_LOW_PRIORITY_INTERVAL,
    DEFAULT_ROUTE_RULES,
    PROACTIVE_BOT_BUDGETS,
    MessageQuota,
    RateLimitBudget,
    RateLimiter,
    RequestPriority,
    RouteRule,
    _PriorityGate,
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


class ManualClock:
    """``sleep`` 只在测试调用 :meth:`advance` 时结束，用于确定性排序断言。"""

    def __init__(self):
        self.now = 0.0
        self._sleepers = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + seconds, future))
        await future

    def advance(self, seconds):
        self.now += seconds
        remaining = []
        for deadline, future in self._sleepers:
            if deadline <= self.now:
                if not future.done():
                    future.set_result(None)
            else:
                remaining.append((deadline, future))
        self._sleepers = remaining


class PriorityGateTests(unittest.IsolatedAsyncioTestCase):
    """通道门本身的排序、公平保护与取消安全。"""

    def _spawn(self, gate, priority, order, name):
        async def run():
            await gate.acquire(priority)
            order.append(name)
            gate.release()

        return asyncio.create_task(run())

    async def test_idle_gate_is_taken_without_queueing(self):
        gate = _PriorityGate(None)
        await gate.acquire(RequestPriority.NORMAL)
        self.assertTrue(gate.locked)
        self.assertEqual(0, gate.waiting)
        gate.release()
        self.assertFalse(gate.locked)

    async def test_same_priority_keeps_arrival_order(self):
        gate = _PriorityGate(None)
        order = []
        await gate.acquire(RequestPriority.NORMAL)
        tasks = [self._spawn(gate, RequestPriority.BULK, order, f"b{index}") for index in range(4)]
        await asyncio.sleep(0)
        self.assertEqual(4, gate.waiting)
        gate.release()
        await asyncio.gather(*tasks)
        self.assertEqual(["b0", "b1", "b2", "b3"], order)

    async def test_interactive_overtakes_queued_bulk(self):
        gate = _PriorityGate(None)
        order = []
        await gate.acquire(RequestPriority.NORMAL)
        tasks = [self._spawn(gate, RequestPriority.BULK, order, f"bulk-{index}") for index in range(3)]
        await asyncio.sleep(0)
        # 对话消息后到，但插到队头
        tasks.append(self._spawn(gate, RequestPriority.INTERACTIVE, order, "interactive"))
        await asyncio.sleep(0)
        self.assertEqual(4, gate.waiting)

        gate.release()
        await asyncio.gather(*tasks)

        self.assertEqual(["interactive", "bulk-0", "bulk-1", "bulk-2"], order)

    async def test_normal_priority_overtakes_bulk_too(self):
        gate = _PriorityGate(None)
        order = []
        await gate.acquire(RequestPriority.BULK)
        tasks = [self._spawn(gate, RequestPriority.BULK, order, "bulk")]
        tasks.append(self._spawn(gate, RequestPriority.NORMAL, order, "normal"))
        await asyncio.sleep(0)
        gate.release()
        await asyncio.gather(*tasks)
        self.assertEqual(["normal", "bulk"], order)

    async def test_low_priority_is_released_after_the_fairness_interval(self):
        gate = _PriorityGate(4)
        order = []
        await gate.acquire(RequestPriority.NORMAL)
        tasks = [self._spawn(gate, RequestPriority.BULK, order, "bulk")]
        tasks += [self._spawn(gate, RequestPriority.INTERACTIVE, order, f"i{index}") for index in range(6)]
        await asyncio.sleep(0)
        self.assertEqual(7, gate.waiting)

        gate.release()
        await asyncio.gather(*tasks)

        # 连续放行 4 个高优先级后，强制放行等待最久的低优先级请求。
        self.assertEqual(["i0", "i1", "i2", "i3", "bulk", "i4", "i5"], order)

    async def test_fairness_can_be_disabled(self):
        gate = _PriorityGate(None)
        order = []
        await gate.acquire(RequestPriority.NORMAL)
        tasks = [self._spawn(gate, RequestPriority.BULK, order, "bulk")]
        tasks += [self._spawn(gate, RequestPriority.INTERACTIVE, order, f"i{index}") for index in range(6)]
        await asyncio.sleep(0)

        gate.release()
        await asyncio.gather(*tasks)

        self.assertEqual(["i0", "i1", "i2", "i3", "i4", "i5", "bulk"], order)

    async def test_waiting_by_priority_is_reported(self):
        gate = _PriorityGate(None)
        await gate.acquire(RequestPriority.NORMAL)
        tasks = [
            self._spawn(gate, RequestPriority.BULK, [], "b1"),
            self._spawn(gate, RequestPriority.BULK, [], "b2"),
            self._spawn(gate, RequestPriority.INTERACTIVE, [], "i1"),
        ]
        await asyncio.sleep(0)

        self.assertEqual({"interactive": 1, "normal": 0, "bulk": 2}, gate.waiting_by_priority())

        gate.release()
        await asyncio.gather(*tasks)

    async def test_cancelling_a_queued_waiter_leaves_the_rest_intact(self):
        gate = _PriorityGate(None)
        order = []
        await gate.acquire(RequestPriority.NORMAL)
        doomed = self._spawn(gate, RequestPriority.BULK, order, "doomed")
        survivors = [self._spawn(gate, RequestPriority.BULK, order, name) for name in ("s1", "s2")]
        await asyncio.sleep(0)
        self.assertEqual(3, gate.waiting)

        doomed.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await doomed
        self.assertEqual(2, gate.waiting)

        gate.release()
        await asyncio.gather(*survivors)
        self.assertEqual(["s1", "s2"], order)

    async def test_cancelling_a_granted_waiter_hands_the_gate_over(self):
        gate = _PriorityGate(None)
        order = []
        await gate.acquire(RequestPriority.NORMAL)
        granted = self._spawn(gate, RequestPriority.BULK, order, "granted")
        await asyncio.sleep(0)

        gate.release()  # 所有权已移交，但任务尚未恢复
        granted.cancel()  # 恢复前取消

        with self.assertRaises(asyncio.CancelledError):
            await granted

        # 所有权必须已经被交还，否则整个通道会永久卡死。
        self.assertFalse(gate.locked)
        self.assertEqual(0, gate.waiting)
        await asyncio.wait_for(gate.acquire(RequestPriority.NORMAL), timeout=1)
        self.assertTrue(gate.locked)
        gate.release()


class PriorityDerivationTests(unittest.TestCase):
    """按请求语义自动判定优先级。"""

    def _limiter(self, **kwargs):
        return RateLimiter(**kwargs)

    def _priority(self, limiter, path, body):
        planned, priority = limiter._resolve("POST", path, json_body=body, route_template=None, priority=None)
        self.assertTrue(planned, "消息接口应当至少占用一个通道")
        return priority

    def test_passive_reply_is_interactive(self):
        limiter = self._limiter()
        for path in ("/v2/groups/G1/messages", "/v2/users/U1/messages", "/channels/C1/messages"):
            with self.subTest(path=path):
                self.assertEqual(RequestPriority.INTERACTIVE, self._priority(limiter, path, {"msg_id": "m"}))

    def test_event_id_also_marks_interactive(self):
        limiter = self._limiter()
        self.assertEqual(
            RequestPriority.INTERACTIVE, self._priority(limiter, "/v2/groups/G1/messages", {"event_id": "e"})
        )

    def test_proactive_push_is_bulk(self):
        limiter = self._limiter()
        for path in ("/v2/groups/G1/messages", "/v2/users/U1/messages", "/channels/C1/messages"):
            with self.subTest(path=path):
                self.assertEqual(RequestPriority.BULK, self._priority(limiter, path, {"content": "push"}))

    def test_non_message_route_is_normal(self):
        limiter = self._limiter()
        planned, priority = limiter._resolve("GET", "/users/@me", json_body=None, route_template=None, priority=None)
        self.assertEqual(RequestPriority.NORMAL, priority)
        self.assertTrue(planned)

    def test_unknown_route_is_normal(self):
        limiter = self._limiter()
        _, priority = limiter._resolve("GET", "/brand/new/path", json_body=None, route_template=None, priority=None)
        self.assertEqual(RequestPriority.NORMAL, priority)

    def test_explicit_priority_wins(self):
        limiter = self._limiter()
        _, priority = limiter._resolve(
            "POST",
            "/v2/groups/G1/messages",
            json_body={"content": "push"},
            route_template=None,
            priority=RequestPriority.INTERACTIVE,
        )
        self.assertEqual(RequestPriority.INTERACTIVE, priority)

    def test_no_priority_can_be_expressed_by_leaving_body_unreadable(self):
        # 非 Mapping 的消息体无法判定被动回复，按批量推送处理。
        limiter = self._limiter()
        self.assertEqual(RequestPriority.BULK, self._priority(limiter, "/v2/groups/G1/messages", None))

    def test_invalid_low_priority_interval_is_rejected(self):
        for value in (0, -1, True, "4", 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RateLimiter(low_priority_interval=value)

    def test_default_interval_is_used(self):
        self.assertEqual(DEFAULT_LOW_PRIORITY_INTERVAL, RateLimiter().low_priority_interval)
        self.assertIsNone(RateLimiter(low_priority_interval=None).low_priority_interval)


class QueuePriorityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """整流器层的端到端排队顺序。"""

    async def _wait_until(self, predicate, message):
        for _ in range(500):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail(message)

    def _limiter(self, clock, budgets):
        return RateLimiter(
            clock=clock,
            sleep=clock.sleep,
            route_rules={"POST /demo": RouteRule(tuple(budgets))},
        )

    def _message_limiter(self, clock):
        """带 ``MessageQuota`` 的消息路由：优先级才会按 msg_id 自动判定。"""

        return RateLimiter(
            clock=clock,
            sleep=clock.sleep,
            route_rules={
                "POST /demo": RouteRule(
                    (RateLimitBudget(1, 1.0),),
                    MessageQuota(relationship_budgets=(RateLimitBudget(1, 1.0),)),
                )
            },
        )

    async def test_passive_reply_overtakes_queued_proactive_pushes(self):
        clock = ManualClock()
        limiter = self._limiter(clock, [RateLimitBudget(1, 1.0)])
        key = ("route", "POST /demo")
        order = []

        async def worker(name, priority):
            await limiter.acquire("POST", "/demo", priority=priority)
            order.append(name)

        # 先消耗掉桶里的初始令牌，后续请求才会排队。
        await limiter.acquire("POST", "/demo", priority=RequestPriority.BULK)
        first = asyncio.create_task(worker("bulk-1", RequestPriority.BULK))
        await asyncio.sleep(0)
        await self._wait_until(lambda: limiter._channels[key].gate.locked, "首个请求未占用通道")
        queued = [asyncio.create_task(worker(name, RequestPriority.BULK)) for name in ("bulk-2", "bulk-3")]
        await self._wait_until(lambda: limiter._channels[key].gate.waiting >= 2, "批量请求未排队")
        # 时间间隔内进来一条对话消息，应当插到队头
        conversational = asyncio.create_task(worker("interactive", RequestPriority.INTERACTIVE))
        await self._wait_until(lambda: limiter._channels[key].gate.waiting >= 3, "对话消息未排队")

        for _ in range(6):
            clock.advance(1.0)
            for _ in range(10):
                await asyncio.sleep(0)
        await asyncio.gather(first, *queued, conversational)

        # bulk-1 已经在取令牌，不会被抢占；但对话消息插在其他批量推送前面。
        self.assertEqual(["bulk-1", "interactive", "bulk-2", "bulk-3"], order)

    async def test_priority_is_derived_from_the_body_without_explicit_priority(self):
        clock = ManualClock()
        limiter = self._message_limiter(clock)
        key = ("route", "POST /demo")
        order = []

        async def worker(name, body):
            await limiter.acquire("POST", "/demo", json_body=body)
            order.append(name)

        await limiter.acquire("POST", "/demo", json_body={"content": "warmup"})
        first_push = asyncio.create_task(worker("push-1", {"content": "push"}))
        await asyncio.sleep(0)
        await self._wait_until(lambda: limiter._channels[key].gate.locked, "主动推送未占用通道")
        queued_push = asyncio.create_task(worker("push-2", {"content": "push"}))
        await self._wait_until(lambda: limiter._channels[key].gate.waiting >= 1, "第二个主动推送未排队")
        # 带 msg_id 的对话消息后到，但应当插到批量推送前面。
        reply = asyncio.create_task(worker("reply", {"content": "hi", "msg_id": "m"}))
        await self._wait_until(lambda: limiter._channels[key].gate.waiting >= 2, "对话消息未排队")

        for _ in range(4):
            clock.advance(1.0)
            for _ in range(10):
                await asyncio.sleep(0)
        await asyncio.gather(first_push, queued_push, reply)

        self.assertEqual(["push-1", "reply", "push-2"], order)

    async def test_snapshot_reports_waiting_by_priority(self):
        clock = ManualClock()
        limiter = self._limiter(clock, [RateLimitBudget(1, 1.0)])
        key = ("route", "POST /demo")

        async def worker(priority):
            await limiter.acquire("POST", "/demo", priority=priority)

        # 先消耗掉桶里的初始令牌，后续请求才会排队。
        await limiter.acquire("POST", "/demo", priority=RequestPriority.NORMAL)
        holder = asyncio.create_task(worker(RequestPriority.NORMAL))
        await asyncio.sleep(0)
        await self._wait_until(lambda: limiter._channels[key].gate.locked, "首个请求未占用通道")
        tasks = [asyncio.create_task(worker(RequestPriority.BULK)) for _ in range(2)]
        tasks.append(asyncio.create_task(worker(RequestPriority.INTERACTIVE)))
        await self._wait_until(lambda: limiter._channels[key].gate.waiting >= 3, "等待者未排队")

        entry = next(item for item in limiter.snapshot()["channels"] if item["key"] == "route POST /demo")
        self.assertEqual({"interactive": 1, "normal": 0, "bulk": 2}, entry["waiting_by_priority"])

        holder.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(holder, *tasks, return_exceptions=True)


class RecordingLimiter(RateLimiter):
    """记录 ``acquire`` 的调用、优先级与请求体，不做真正的限速。"""

    def __init__(self):
        self.calls = []
        self.bodies = []

    async def acquire(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get("priority")))
        self.bodies.append(kwargs.get("json_body"))

    async def acquire_slot(self):
        return None

    def release_slot(self):
        return None


class PriorityPlumbingTests(unittest.IsolatedAsyncioTestCase):
    """显式优先级要能穿过 ApiClient / BotHttp / ClientAPI。"""

    async def test_api_client_forwards_priority(self):
        limiter = RecordingLimiter()
        session = FakeSession([FakeResponse(payload={"ok": True})])
        client = ApiClient(FakeTokenProvider(), session=session, rate_limiter=limiter)

        await client.request("GET", "/users/@me", priority=RequestPriority.INTERACTIVE)

        self.assertEqual([("GET", "/users/@me", RequestPriority.INTERACTIVE)], limiter.calls)

    async def test_api_client_defaults_to_derived_priority(self):
        limiter = RecordingLimiter()
        session = FakeSession([FakeResponse(payload={"ok": True})])
        client = ApiClient(FakeTokenProvider(), session=session, rate_limiter=limiter)

        await client.request("GET", "/users/@me")

        # None 表示交给整流器按请求语义判定。
        self.assertEqual([("GET", "/users/@me", None)], limiter.calls)

    async def test_bot_http_forwards_priority(self):
        limiter = RecordingLimiter()
        http = BotHttp(timeout=5, app_id="app", secret="secret")
        http._token = FakeBotToken()
        http._client = ApiClient(
            FakeTokenProvider(),
            session=FakeSession([FakeResponse(payload={"id": "1"})]),
            rate_limiter=limiter,
        )

        route = Route("POST", "/v2/groups/{group_openid}/messages", group_openid="AAA")
        await http.request(route, json={"msg_type": 0}, priority=RequestPriority.INTERACTIVE)

        self.assertEqual([("POST", "/v2/groups/AAA/messages", RequestPriority.INTERACTIVE)], limiter.calls)
        await http.close()

    async def test_client_api_request_forwards_priority(self):
        captured = {}

        class RecordingHttp:
            async def request(self, route, **kwargs):
                captured["route"] = route
                captured["priority"] = kwargs.get("priority")
                return {"ok": True}

        api = BotAPI(http=RecordingHttp())
        await api.request("GET", "/users/@me", priority=RequestPriority.BULK)

        self.assertEqual(RequestPriority.BULK, captured["priority"])
        self.assertEqual("/users/@me", captured["route"].path)

    async def test_client_send_passes_the_body_priority_derivation_needs(self):
        limiter = RecordingLimiter()
        http = BotHttp(timeout=5, app_id="app", secret="secret")
        http._token = FakeBotToken()
        http._client = ApiClient(
            FakeTokenProvider(),
            session=FakeSession([FakeResponse(payload={"id": "1"}), FakeResponse(payload={"id": "2"})]),
            rate_limiter=limiter,
        )
        client = Client(Intents.none(), bot_log=None)
        client.http = http
        client.api._http = http
        try:
            await Client.send(
                client,
                ReplyTarget(scope="group", target_id="group-1", message_id="inbound"),
                content="hi",
            )
            await Client.send(client, ReplyTarget(scope="group", target_id="group-1"), content="push")
        finally:
            await client.close()

        # 自动判定依赖请求体里的 msg_id/event_id，这里确认它确实被送到了整流器。
        self.assertIn("msg_id", limiter.bodies[0])
        self.assertNotIn("msg_id", limiter.bodies[1])


class ChannelRegistryTests(unittest.IsolatedAsyncioTestCase):
    """LRU 回收不能丢掉尚未恢复的配额状态。"""

    def _limiter(self, clock, **kwargs):
        return RateLimiter(
            clock=clock,
            sleep=clock.sleep,
            route_rules={
                "POST /demo/{}/messages": RouteRule(
                    (RateLimitBudget(1000, 1.0),),
                    MessageQuota(relationship_budgets=(RateLimitBudget(1, 60.0),)),
                )
            },
            **kwargs,
        )

    @staticmethod
    async def _send(limiter, resource):
        return await limiter.acquire("POST", f"/demo/{resource}/messages", json_body={"content": "push"})

    @staticmethod
    def _key(resource):
        return ("proactive", "POST /demo/{}/messages", resource)

    async def test_consumed_channels_survive_eviction(self):
        clock = FakeClock()
        limiter = self._limiter(clock, max_buckets=2)
        for resource in ("R0", "R1", "R2"):
            await self._send(limiter, resource)

        # 三个通道的桶都还有已消耗的状态，回收任何一个都会把配额静默清零，
        # 因此注册表允许超过 max_buckets。
        self.assertIn(self._key("R0"), limiter._channels)
        self.assertGreater(len(limiter._channels), 2)

    async def test_recycled_channel_does_not_reset_the_quota(self):
        clock = FakeClock()
        limiter = self._limiter(clock, max_buckets=1)
        await self._send(limiter, "R0")
        for resource in ("R1", "R2", "R3"):
            await self._send(limiter, resource)

        self.assertIn(self._key("R0"), limiter._channels)
        before = clock.now
        await self._send(limiter, "R0")  # 必须等满 60 秒，而不是拿到一个新满桶
        self.assertAlmostEqual(60.0, clock.now - before, places=3)

    async def test_fully_recovered_channels_are_recycled(self):
        clock = FakeClock()
        limiter = self._limiter(clock, max_buckets=2)
        for resource in ("R0", "R1", "R2"):
            await self._send(limiter, resource)

        clock.now += 3600.0  # 所有桶补满，丢弃无损
        await self._send(limiter, "R3")

        self.assertLessEqual(len(limiter._channels), 2)

    async def test_penalised_channel_is_not_recycled(self):
        clock = FakeClock()
        limiter = self._limiter(clock, max_buckets=2)
        for resource in ("R0", "R1", "R2"):
            await self._send(limiter, resource)

        clock.now += 3600.0
        limiter.notify_rate_limited("POST", "/demo/R0/messages", json_body={"content": "push"}, retry_after=600)
        await self._send(limiter, "R3")

        # 惩罚还在生效，回收等于把 429 退避一起丢掉。
        self.assertIn(self._key("R0"), limiter._channels)


class VirtualClock:
    """虚拟时钟：``sleep`` 挂起到到期，由 :func:`drive` 推进并**同时**唤醒到期任务。

    与 :class:`ManualClock` 的区别是它会真正到期放行，因此可以断言"实际发出时刻"。
    不能让每个 ``sleep`` 各自推进时钟，否则会掩盖并发请求之间的顺序问题。
    """

    def __init__(self):
        self.now = 0.0
        self._waiters = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self._waiters.append((self.now + seconds, future))
        await future

    async def advance(self, seconds):
        self.now += seconds
        due, pending = [], []
        for deadline, future in self._waiters:
            (due if deadline <= self.now else pending).append((deadline, future))
        self._waiters = pending
        for _, future in due:
            if not future.done():
                future.set_result(None)


async def drive(clock, tasks, horizon=400.0, step=0.02, yields=10):
    """推进虚拟时间，直到 ``tasks`` 全部结束。"""

    for _ in range(int(horizon / step) + 10):
        if all(task.done() for task in tasks):
            return
        await clock.advance(step)
        for _ in range(yields):
            await asyncio.sleep(0)
    raise AssertionError("drive 超时")


class SessionSettings:
    """``httpx.AsyncClient`` 替身所需的最小状态。"""

    def __init__(self, clock, first_duration=0.01):
        self.is_closed = False
        self.calls = []
        self._clock = clock
        self._first_duration = first_duration

    async def request(self, method, url, **kwargs):
        self.calls.append(round(self._clock.now, 3))
        await self._clock.sleep(self._first_duration if len(self.calls) == 1 else 0.01)

        class Response:
            status_code = 200
            headers = {}

            @property
            def text(self):
                return "{}"

        return Response()

    async def close(self):
        self.is_closed = True


class SendTimingRegressionTests(unittest.IsolatedAsyncioTestCase):
    """断言真实发出时刻，而不只是断言桶的剩余令牌或并发数量。"""

    async def test_concurrency_slot_does_not_stack_rate_tokens(self):
        """并发槽位必须在限流之前获取，否则等待槽位期间会堆积令牌并成串发出。"""

        clock = VirtualClock()
        limiter = RateLimiter(
            clock=clock,
            sleep=clock.sleep,
            max_concurrency=1,
            route_rules={"GET /demo": RouteRule((RateLimitBudget(1, 1.0),))},
        )
        session = SessionSettings(clock, first_duration=4.0)
        client = ApiClient(FakeTokenProvider(), session=session, rate_limiter=limiter)

        tasks = [asyncio.create_task(client.request("GET", "/demo")) for _ in range(5)]
        await drive(clock, tasks, horizon=60)

        gaps = [round(session.calls[index + 1] - session.calls[index], 3) for index in range(len(session.calls) - 1)]
        # 令牌速率 1/s：除首个慢请求占住槽位的那一段外，其余发送间隔不得小于 1 秒。
        self.assertTrue(all(gap >= 0.99 for gap in gaps), gaps)

    async def test_multi_channel_waits_keep_the_bot_dimension_rate(self):
        """先耗尽单关系配额，再各发一条：Bot 维度 1/s 的间隔必须保持。"""

        clock = VirtualClock()
        limiter = RateLimiter(
            clock=clock,
            sleep=clock.sleep,
            route_rules={
                "POST /demo/{}/messages": RouteRule(
                    (RateLimitBudget(1000, 1.0),),
                    MessageQuota(
                        bot_budgets=(RateLimitBudget(1, 1.0),),
                        relationship_budgets=(RateLimitBudget(1, 10.0),),
                    ),
                )
            },
        )
        sent = []

        async def send(resource):
            await limiter.acquire("POST", f"/demo/{resource}/messages", json_body={"content": "push"})
            sent.append(clock.now)

        resources = ("A", "B", "C")
        await drive(clock, [asyncio.create_task(send(r)) for r in resources], horizon=100)
        await drive(clock, [asyncio.create_task(send(r)) for r in resources], horizon=300)

        self.assertEqual(6, len(sent))
        for index in range(1, len(sent)):
            self.assertGreaterEqual(round(sent[index] - sent[index - 1], 3), 0.99, sent)

    async def test_client_close_during_rate_limit_wait_aborts_send(self):
        """限流等待期间客户端被关闭，放行后不得再向已关闭的 session 发请求。"""

        clock = VirtualClock()
        limiter = RateLimiter(
            clock=clock,
            sleep=clock.sleep,
            route_rules={"GET /demo": RouteRule((RateLimitBudget(1, 10.0),))},
        )
        session = SessionSettings(clock)
        # 退避 sleep 也走虚拟时钟，否则关闭后的重试会依赖真实等待。
        client = ApiClient(FakeTokenProvider(), session=session, rate_limiter=limiter, sleep=clock.sleep)

        warmup = asyncio.create_task(client.request("GET", "/demo"))  # 消耗掉唯一的令牌
        await drive(clock, [warmup], horizon=60)
        self.assertEqual(1, len(session.calls))

        pending = asyncio.create_task(client.request("GET", "/demo"))
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertFalse(pending.done(), "第二个请求应当停在限流等待里")

        await client.close()
        await drive(clock, [pending], horizon=60)

        self.assertEqual(1, len(session.calls), "关闭后不应再发出请求")
        outcomes = await asyncio.gather(pending, return_exceptions=True)
        self.assertIsInstance(outcomes[0], BaseException)
