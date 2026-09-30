# -*- coding: utf-8 -*-
"""出站请求的速率整流（rate limiting）与排队。

QQ 开放平台对每个 REST 接口都声明了调用频率上限（``接口频率限制``），消息类接口
还会额外叠加「主动消息」的 Bot 维度、单关系维度与每日额度限制。突发流量下即使
平均值达标，瞬时并发也极易触发 ``HTTP 429``，因此 SDK 在传输层之上引入一个
**队列化的令牌桶**：

* 每个「逻辑通道」（接口、单关系、Bot 维度）各自持有独立的令牌桶；
* 同一个通道上的请求通过 ``asyncio.Lock`` 串行排队，先到先发，天然整流；
* 收到 ``429`` 时按 ``Retry-After`` 对该通道追加惩罚时间，避免继续撞墙；
* 未在文档中标注限制的接口回落到保守的同族默认值，保证不会无限速发出。

限制值来源与核对过程见 ``generated_docs/RATE_LIMITS.md``。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "RateLimitBudget",
    "RateLimiter",
    "RouteRule",
    "MessageQuota",
    "DEFAULT_ROUTE_RULES",
    "DEFAULT_BUDGETS",
    "DEFAULT_MAX_BUCKETS",
    "DEFAULT_MAX_PACING_WINDOW",
    "DEFAULT_PENALTY_SECONDS",
    "normalise_template",
    "match_route",
]

DEFAULT_MAX_BUCKETS = 4096
DEFAULT_PENALTY_SECONDS = 1.0
#: 窗口长于该值的配额（例如「每天 1000 条」）只做提示而不阻塞发送。
DEFAULT_MAX_PACING_WINDOW = 3600.0
_DAILY_WINDOW = 86400.0

_QUANTITY_RE = re.compile(r"(?P<count>\d+)\s*/?\s*(?P<unit>QPS|QPM)", re.IGNORECASE)
_UNIT_WINDOW = {"qps": 1.0, "qpm": 60.0}
# ``5/qps & 30/qpm`` 用 ``/`` 连接数量与单位，因此分隔符只认带空格的 ``/``。
_SEPARATOR_RE = re.compile(r"&|,|，|、|\s/\s")
_PARAMETER_RE = re.compile(r"\{[^}]*\}")


def normalise_template(path: str) -> str:
    """把路由模板中的参数名统一成 ``{}``，便于跨模块比对。"""

    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    normalised = _PARAMETER_RE.sub("{}", path.strip())
    if not normalised.startswith("/"):
        normalised = "/" + normalised.lstrip("/")
    return normalised.rstrip("/") or "/"


def _segments(path: str) -> Tuple[str, ...]:
    # 路由匹配会对限制表里的每个模板调用一次，缓存后单次请求的开销可以忽略。
    return _segments_cached(path)


@lru_cache(maxsize=2048)
def _segments_cached(path: str) -> Tuple[str, ...]:
    return tuple(segment for segment in path.strip("/").split("/") if segment)


@dataclass(frozen=True)
class RateLimitBudget:
    """单个令牌桶的配额：``limit`` 次请求 / ``window`` 秒。

    ``burst`` 用于文档中的 ``2 QPM / 10 QPM burst`` 形式：桶容量为 ``burst``，
    但补充速率仍按 ``limit / window`` 计算。
    """

    limit: int
    window: float
    burst: Optional[int] = None

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit <= 0:
            raise ValueError("limit must be a positive integer")
        if isinstance(self.window, bool) or not isinstance(self.window, (int, float)) or self.window <= 0:
            raise ValueError("window must be a positive number of seconds")
        if self.burst is not None:
            if isinstance(self.burst, bool) or not isinstance(self.burst, int) or self.burst < self.limit:
                raise ValueError("burst must be an integer greater than or equal to limit")

    @property
    def capacity(self) -> float:
        return float(self.burst if self.burst is not None else self.limit)

    @property
    def rate(self) -> float:
        """每秒补充的令牌数。"""

        return self.limit / float(self.window)

    @property
    def interval(self) -> float:
        """稳定状态下两次请求之间的最小间隔（秒）。"""

        return 1.0 / self.rate

    @classmethod
    def parse(cls, text: str) -> Tuple["RateLimitBudget", ...]:
        """解析文档原文，例如 ``"5/qps & 30/qpm"``、``"2 QPM / 10 QPM burst"``。"""

        if not isinstance(text, str) or not text.strip():
            raise ValueError("limit text must be a non-empty string")
        budgets: List[RateLimitBudget] = []
        burst: Optional[int] = None
        # 逐段解析，避免 "2 QPM / 10 QPM burst" 中前一段被后一段的 burst 说明误导。
        for part in _SEPARATOR_RE.split(text):
            match = _QUANTITY_RE.search(part)
            if match is None:
                continue
            count = int(match.group("count"))
            window = _UNIT_WINDOW[match.group("unit").casefold()]
            if "burst" in part.casefold():
                burst = count
            else:
                budgets.append(cls(count, window))
        if not budgets:
            if burst is None:
                raise ValueError(f"no rate limit found in {text!r}")
            # 只写了 burst 容量的退化写法，按同一窗口当作持续速率使用。
            return (cls(burst, 60.0),)
        if burst is not None:
            budgets[0] = replace(budgets[0], burst=max(burst, budgets[0].limit))
        return tuple(budgets)

    def describe(self) -> str:
        suffix = f" burst={self.burst}" if self.burst is not None else ""
        return f"{self.limit}/{self.window:g}s{suffix}"


@dataclass(frozen=True)
class MessageQuota:
    """消息类接口额外叠加的主动消息配额。

    ``bot_budgets`` 是 Bot 维度的全局配额，``relationship_budgets`` 按接收方
    （群 / 用户 / 子频道）单独计数，两者都只作用于主动消息；``always_budgets``
    则对主动与被动消息同时生效（例如子频道每秒 5 条）。
    """

    bot_budgets: Tuple[RateLimitBudget, ...] = ()
    relationship_budgets: Tuple[RateLimitBudget, ...] = ()
    always_budgets: Tuple[RateLimitBudget, ...] = ()


@dataclass(frozen=True)
class RouteRule:
    """单个接口的整流规则。"""

    budgets: Tuple[RateLimitBudget, ...] = ()
    quota: Optional[MessageQuota] = None

    def describe(self) -> str:
        parts = [budget.describe() for budget in self.budgets]
        return " + ".join(parts) if parts else "-"


# --------------------------------------------------------------------------- #
# 文档核对后的默认限制表
# --------------------------------------------------------------------------- #
# 每一条都对应 generated_docs/api-v2 中的「接口频率限制」字段，注释保留文档原文。
DEFAULT_ROUTE_RULES: Dict[str, RouteRule] = {
    # -- 频道 / 子频道 / 用户（openapi 旧接口族，文档统一 50 QPS） -------------
    "GET /users/@me": RouteRule((RateLimitBudget(50, 1.0),)),  # 50 QPS
    "GET /users/@me/guilds": RouteRule((RateLimitBudget(50, 1.0),)),  # 50 QPS
    "POST /users/@me/dms": RouteRule((RateLimitBudget(50, 1.0),)),  # 同族默认
    "GET /gateway/bot": RouteRule((RateLimitBudget(2, 60.0, burst=10),)),  # 2 QPM / 10 QPM burst
    "GET /gateway": RouteRule((RateLimitBudget(2, 60.0, burst=10),)),  # 2 QPM / 10 QPM burst
    "GET /guilds/{}": RouteRule((RateLimitBudget(50, 1.0),)),  # 50 QPS
    "GET /guilds/{}/channels": RouteRule((RateLimitBudget(50, 1.0),)),  # 50 QPS
    "POST /guilds/{}/channels": RouteRule((RateLimitBudget(50, 1.0),)),  # 50 QPS
    "GET /channels/{}": RouteRule((RateLimitBudget(50, 1.0),)),  # 50 QPS
    "PATCH /channels/{}": RouteRule((RateLimitBudget(50, 1.0),)),  # 50 QPS
    "DELETE /channels/{}": RouteRule((RateLimitBudget(50, 1.0),)),  # 50 QPS
    "PUT /interactions/{}": RouteRule((RateLimitBudget(50, 1.0),)),  # 50 QPS
    # -- 消息 ---------------------------------------------------------------
    # 100 QPS（接口级）；被动回复 5 分钟 / 5 次；主动消息另有 Bot 与单群配额，
    # 具体档位由 ``certification`` 决定，见 PROACTIVE_BOT_BUDGETS。
    "POST /v2/groups/{}/messages": RouteRule(
        (RateLimitBudget(100, 1.0),),
        MessageQuota(
            relationship_budgets=(RateLimitBudget(20, 60.0), RateLimitBudget(1000, _DAILY_WINDOW)),
        ),
    ),
    # 100 QPS，包括主动、被动等所有消息类型。
    "POST /v2/users/{}/messages": RouteRule(
        (RateLimitBudget(100, 1.0),),
        MessageQuota(
            relationship_budgets=(RateLimitBudget(20, 60.0), RateLimitBudget(1000, _DAILY_WINDOW)),
        ),
    ),
    "POST /v2/users/{}/stream_messages": RouteRule((RateLimitBudget(50, 1.0),)),  # 50 QPS
    "DELETE /v2/users/{}/messages/{}": RouteRule((RateLimitBudget(10, 1.0),)),  # 10 QPS
    "DELETE /v2/groups/{}/messages/{}": RouteRule((RateLimitBudget(10, 1.0),)),  # 10 QPS
    # 子频道：每 1s 最多 5 条（主动+被动）；主动推送默认每天每子频道 20 条。
    "POST /channels/{}/messages": RouteRule(
        (RateLimitBudget(50, 1.0),),
        MessageQuota(
            relationship_budgets=(RateLimitBudget(20, _DAILY_WINDOW),),
            always_budgets=(RateLimitBudget(5, 1.0),),
        ),
    ),
    # 私信：每个机器人每天对一个用户 2 条、累计 200 条主动消息。
    "POST /dms/{}/messages": RouteRule(
        (RateLimitBudget(50, 1.0),),
        MessageQuota(
            bot_budgets=(RateLimitBudget(200, _DAILY_WINDOW),),
            relationship_budgets=(RateLimitBudget(2, _DAILY_WINDOW),),
        ),
    ),
    # -- 富媒体上传 ----------------------------------------------------------
    "POST /v2/groups/{}/files": RouteRule((RateLimitBudget(50, 1.0),)),  # 50 QPS
    "POST /v2/users/{}/files": RouteRule((RateLimitBudget(50, 1.0),)),  # 50 QPS
    "POST /v2/groups/{}/upload_prepare": RouteRule((RateLimitBudget(10, 1.0),)),  # 10 QPS
    "POST /v2/groups/{}/upload_part_finish": RouteRule((RateLimitBudget(10, 1.0),)),  # 10 QPS
    "POST /v2/users/{}/upload_prepare": RouteRule((RateLimitBudget(10, 1.0),)),  # 10 QPS
    "POST /v2/users/{}/upload_part_finish": RouteRule((RateLimitBudget(10, 1.0),)),  # 10 QPS
    "POST /v2/generate_url_link": RouteRule((RateLimitBudget(50, 1.0),)),  # 50 QPS
    # -- 群管理 --------------------------------------------------------------
    "GET /v2/groups/{}/bot_state": RouteRule((RateLimitBudget(30, 60.0),)),  # 30 QPM
    "GET /v2/groups/{}/info": RouteRule((RateLimitBudget(30, 60.0),)),  # 30 QPM
    "GET /v2/groups/{}/members": RouteRule((RateLimitBudget(60, 60.0),)),  # 60 QPM
    "GET /v2/groups/{}/members/{}": RouteRule((RateLimitBudget(30, 60.0),)),  # 30 QPM
    "GET /v2/groups/{}/join_request_list": RouteRule((RateLimitBudget(30, 60.0),)),  # 30 QPM
    "GET /v2/groups/{}/member_blacklist": RouteRule((RateLimitBudget(30, 60.0),)),  # 30 QPM
    "POST /v2/groups/{}/member_blacklist": RouteRule((RateLimitBudget(60, 60.0),)),  # 60 QPM
    "POST /v2/groups/{}/batch_remove_members": RouteRule((RateLimitBudget(30, 60.0),)),  # 30 QPM
    "GET /v2/groups/{}/restrict_chat_setting": RouteRule((RateLimitBudget(30, 60.0),)),  # 30 QPM
    "POST /v2/groups/{}/restrict_chat_setting": RouteRule((RateLimitBudget(60, 60.0),)),  # 60 QPM
    "POST /v2/groups/{}/approval_join_request/{}": RouteRule((RateLimitBudget(60, 60.0),)),  # 60 QPM
    # -- 入群审批策略 --------------------------------------------------------
    "GET /v2/groups/join_approval_strategy": RouteRule((RateLimitBudget(60, 60.0),)),  # 60 QPM
    "POST /v2/groups/join_approval_strategy": RouteRule((RateLimitBudget(60, 60.0),)),  # 60 QPM
    "DELETE /v2/groups/join_approval_strategy/{}": RouteRule((RateLimitBudget(60, 60.0),)),  # 60 QPM
    "PATCH /v2/groups/join_approval_strategy/{}": RouteRule((RateLimitBudget(60, 60.0),)),  # 60 QPM
    "POST /v2/groups/join_approval_strategy/{}/execute": RouteRule((RateLimitBudget(60, 60.0),)),  # 60 QPM
    "POST /v2/groups/join_approval_strategy/{}/whitelist_users": RouteRule((RateLimitBudget(60, 60.0),)),  # 60 QPM
    # -- 菜单 / 面板 ---------------------------------------------------------
    "GET /v2/menu": RouteRule((RateLimitBudget(30, 60.0),)),  # 30 QPM
    "PUT /v2/menu": RouteRule((RateLimitBudget(5, 60.0),)),  # 5 QPM
    "GET /v2/panels": RouteRule((RateLimitBudget(30, 60.0),)),  # 30 QPM
    "POST /v2/panels": RouteRule((RateLimitBudget(10, 60.0),)),  # 10 QPM
    "GET /v2/panels/{}": RouteRule((RateLimitBudget(30, 60.0),)),  # 30 QPM
    "PUT /v2/panels/{}": RouteRule((RateLimitBudget(10, 60.0),)),  # 10 QPM
    "DELETE /v2/panels/{}": RouteRule((RateLimitBudget(10, 60.0),)),  # 10 QPM
    "PUT /v2/panels/{}/target": RouteRule((RateLimitBudget(60, 60.0),)),  # 60 QPM
}

#: 未在文档中单独标注频率的接口按同族保守值整流。
DEFAULT_BUDGETS: Tuple[RateLimitBudget, ...] = (RateLimitBudget(50, 1.0),)

#: 认证等级决定主动消息的 Bot 维度配额；默认按「未认证」这一最保守档位。
PROACTIVE_BOT_BUDGETS: Dict[str, Dict[str, Tuple[RateLimitBudget, ...]]] = {
    "unverified": {
        "c2c": (RateLimitBudget(5, 1.0), RateLimitBudget(30, 60.0)),
        "group": (RateLimitBudget(30, 60.0),),
    },
    "certified": {
        "c2c": (RateLimitBudget(10, 1.0),),
        "group": (RateLimitBudget(60, 60.0),),
    },
}

_PASSIVE_MARKERS = ("msg_id", "event_id")
_QUOTA_KINDS = {
    "POST /v2/groups/{}/messages": "group",
    "POST /v2/users/{}/messages": "c2c",
}


def match_route(method: str, path: str, table: Mapping[str, RouteRule]) -> Optional[str]:
    """把具体请求路径匹配回文档中的路由模板，返回表中的键。

    静态片段更多的模板优先，因此 ``/v2/groups/join_approval_strategy`` 不会被
    ``/v2/groups/{}/...`` 抢走。
    """

    wanted = _segments(path)
    best: Optional[str] = None
    best_score = -1
    for key in table:
        try:
            key_method, template = key.split(" ", 1)
        except ValueError:  # pragma: no cover - 表由本模块维护
            continue
        if key_method != method:
            continue
        candidates = _segments(template)
        if len(candidates) != len(wanted):
            continue
        score = 0
        for candidate, actual in zip(candidates, wanted):
            if candidate == "{}":
                continue
            if candidate != actual:
                break
            score += 1
        else:
            if score > best_score:
                best_score = score
                best = key
    return best


class _TokenBucket:
    """按时间连续补充的令牌桶，``delay`` 返回距离下一个令牌的等待秒数。"""

    __slots__ = ("budget", "_clock", "tokens", "updated_at", "blocked_until", "warned")

    def __init__(self, budget: RateLimitBudget, clock: Callable[[], float]) -> None:
        self.budget = budget
        self._clock = clock
        self.tokens = budget.capacity
        self.updated_at = clock()
        self.blocked_until = 0.0
        self.warned = False

    def _refill(self, now: float) -> None:
        elapsed = now - self.updated_at
        if elapsed > 0:
            self.tokens = min(self.budget.capacity, self.tokens + elapsed * self.budget.rate)
            self.updated_at = now

    def delay(self, now: float) -> float:
        self._refill(now)
        if self.tokens >= 1.0:
            self.warned = False
            return max(0.0, self.blocked_until - now)
        missing = (1.0 - self.tokens) / self.budget.rate
        return max(missing, self.blocked_until - now)

    def consume(self, now: float) -> None:
        self._refill(now)
        self.tokens = max(0.0, self.tokens - 1.0)
        self.updated_at = now

    def penalise(self, now: float, seconds: float) -> None:
        self.blocked_until = max(self.blocked_until, now + max(0.0, seconds))
        # 惩罚期内不再保留可用令牌，避免惩罚结束后立刻补发一波突发流量。
        self.tokens = 0.0
        self.updated_at = now


class _Channel:
    """一个逻辑通道：若干令牌桶 + 一把保证先到先发的排队锁。"""

    __slots__ = ("budgets", "lock", "buckets", "pending")

    def __init__(self, budgets: Sequence[RateLimitBudget], clock: Callable[[], float]) -> None:
        self.budgets = tuple(budgets)
        self.lock = asyncio.Lock()
        self.buckets = tuple(_TokenBucket(budget, clock) for budget in budgets)
        self.pending = 0


class RateLimiter:
    """出站请求整流器：按接口 / 单关系 / Bot 维度排队限速。

    Args:
      enabled: 关闭后 :meth:`acquire` 立即返回，行为与未启用整流时一致。
        该开关只在构造时读取，运行期不应修改。
      max_concurrency: 可选的全局在途请求上限；``None`` 表示不限制。
      certification: ``"certified"``（默认，企业/个人认证档位）或 ``"unverified"``，
        决定主动消息的 Bot 维度配额档位。平台没有查询认证等级的接口，未认证的机器人
        必须显式传 ``"unverified"``，否则会按已认证配额发出而触发平台限流。
      route_rules: 覆盖或补充默认限制表，键为 ``"METHOD /path/{}/template"``。
      default_budgets: 未匹配到模板时使用的保守配额。
      max_buckets: 通道注册表上限，超出后按 LRU 回收空闲通道。
      default_penalty: 收到 ``429`` 但响应未带 ``Retry-After`` 时的默认惩罚秒数。
      max_pacing_window: 窗口长于该值的配额视为「每日额度」，只提示不阻塞。
      clock / sleep: 便于测试注入。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        max_concurrency: Optional[int] = None,
        certification: str = "certified",
        route_rules: Optional[Mapping[str, RouteRule]] = None,
        default_budgets: Sequence[RateLimitBudget] = DEFAULT_BUDGETS,
        max_buckets: int = DEFAULT_MAX_BUCKETS,
        default_penalty: float = DEFAULT_PENALTY_SECONDS,
        max_pacing_window: float = DEFAULT_MAX_PACING_WINDOW,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        if certification not in PROACTIVE_BOT_BUDGETS:
            raise ValueError("certification must be 'unverified' or 'certified'")
        if max_concurrency is not None:
            if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency <= 0:
                raise ValueError("max_concurrency must be a positive integer or None")
        if isinstance(max_buckets, bool) or not isinstance(max_buckets, int) or max_buckets <= 0:
            raise ValueError("max_buckets must be a positive integer")
        if default_penalty < 0:
            raise ValueError("default_penalty must not be negative")
        if max_pacing_window <= 0:
            raise ValueError("max_pacing_window must be positive")

        self.enabled = enabled
        self.max_concurrency = max_concurrency
        self.certification = certification
        self.default_penalty = float(default_penalty)
        self.max_buckets = max_buckets
        self.max_pacing_window = float(max_pacing_window)
        self._clock = clock
        self._sleep = sleep
        self._logger = logger or logging.getLogger("botpy.protocol.ratelimit")
        self._rules: Dict[str, RouteRule] = dict(DEFAULT_ROUTE_RULES)
        if route_rules:
            for raw_key, rule in route_rules.items():
                if not isinstance(rule, RouteRule):
                    raise TypeError("route_rules values must be RouteRule instances")
                method, _, template = raw_key.partition(" ")
                if not method or not template:
                    raise ValueError(f"route rule key must look like 'POST /path', got {raw_key!r}")
                self._rules[f"{method.upper()} {normalise_template(template)}"] = rule
        self._default_budgets = tuple(default_budgets)
        self._channels: "OrderedDict[Tuple[Any, ...], _Channel]" = OrderedDict()
        self._semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency else None
        self._log_certification_hint()

    def _log_certification_hint(self) -> None:
        """主动消息配额取决于认证等级，而平台没有提供查询接口。

        默认按「已认证」档位整流；未认证的机器人必须显式切到 ``unverified``，
        否则会按更高的配额发出而真的触发平台限流，所以这里提示一次。
        """

        if not self.enabled:
            return
        budgets = PROACTIVE_BOT_BUDGETS[self.certification]
        summary = "；".join(
            f"{label} {' + '.join(budget.describe() for budget in budgets[kind])}"
            for kind, label in (("c2c", "单聊"), ("group", "群聊"))
            if budgets.get(kind)
        )
        if self.certification == "certified":
            self._logger.info(
                "[botpy] 主动消息按「已认证」档位整流（%s）。若机器人未通过企业/个人认证，"
                "请设置 rate_limit={'certification': 'unverified'}，否则会超出平台配额被限流。",
                summary,
            )
        else:
            self._logger.info("[botpy] 主动消息按「未认证」档位整流（%s）。", summary)

    # -- 公开接口 ---------------------------------------------------------- #
    @property
    def route_rules(self) -> Mapping[str, RouteRule]:
        return dict(self._rules)

    async def acquire(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        route_template: Optional[str] = None,
    ) -> None:
        """在当前协程抢占发送配额；必要时排队等待，返回即代表可以立即发出。"""

        if not self.enabled:
            return
        for key, budgets in self._plan(method, path, json_body=json_body, route_template=route_template):
            await self._acquire_channel(key, budgets)

    async def acquire_slot(self) -> None:
        """占用一个全局在途请求名额（未配置 ``max_concurrency`` 时为空操作）。"""

        if self.enabled and self._semaphore is not None:
            await self._semaphore.acquire()

    def release_slot(self) -> None:
        if self.enabled and self._semaphore is not None:
            self._semaphore.release()

    def notify_rate_limited(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        route_template: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        """平台返回 ``429`` 后追加惩罚时间，让同通道的后续请求一起退避。"""

        if not self.enabled:
            return
        penalty = self.default_penalty if retry_after is None else max(0.0, float(retry_after))
        now = self._clock()
        for key, budgets in self._plan(method, path, json_body=json_body, route_template=route_template):
            channel = self._channel(key, budgets)
            for bucket in channel.buckets:
                bucket.penalise(now, penalty)
        if penalty > 0:
            self._logger.debug(
                "[botpy] 触发平台限流，%s %s 暂停 %.3fs 后重试",
                method,
                path,
                penalty,
            )

    def snapshot(self) -> Dict[str, Any]:
        """返回等待队列快照，便于可观测性集成。"""

        channels = []
        for key, channel in self._channels.items():
            channels.append(
                {
                    "key": " ".join(str(part) for part in key),
                    "pending": channel.pending,
                    "budgets": [budget.describe() for budget in channel.budgets],
                    "available": [round(bucket.tokens, 3) for bucket in channel.buckets],
                }
            )
        return {
            "enabled": self.enabled,
            "certification": self.certification,
            "rules": len(self._rules),
            "channels": channels,
        }

    def plan_for(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        route_template: Optional[str] = None,
    ) -> List[Tuple[Tuple[Any, ...], Tuple[RateLimitBudget, ...]]]:
        """公开 :meth:`_plan`，用于配置核对与测试断言。"""

        return self._plan(method, path, json_body=json_body, route_template=route_template)

    # -- 内部实现 ---------------------------------------------------------- #
    def _plan(
        self,
        method: str,
        path: str,
        *,
        json_body: Any,
        route_template: Optional[str],
    ) -> List[Tuple[Tuple[Any, ...], Tuple[RateLimitBudget, ...]]]:
        method = str(method).upper()
        template = normalise_template(route_template) if route_template else None
        matched = None
        if template is not None:
            candidate = f"{method} {template}"
            matched = candidate if candidate in self._rules else match_route(method, template, self._rules)
        if matched is None:
            matched = match_route(method, path, self._rules)

        planned: "OrderedDict[Tuple[Any, ...], Tuple[RateLimitBudget, ...]]" = OrderedDict()

        def add(key: Tuple[Any, ...], budgets: Sequence[RateLimitBudget]) -> None:
            if not budgets:
                return
            # 同一通道的配额必须合并成一个稳定的集合，否则通道会因为配额变化被
            # 反复重建，令牌桶状态（已消耗的额度）就丢失了。
            existing = planned.get(key, ())
            merged = list(existing)
            for budget in budgets:
                if budget not in merged:
                    merged.append(budget)
            planned[key] = tuple(merged)

        if matched is not None:
            rule = self._rules[matched]
            add(("route", matched), rule.budgets)
        elif self._default_budgets:
            add(("route", f"{method} {normalise_template(path)}"), self._default_budgets)

        if matched is not None:
            quota = self._rules[matched].quota
            if quota is not None:
                resource = self._resource_id(matched, path)
                # 主动 / 被动消息共用接口，但「单关系」配额只对主动消息生效，因此
                # 用独立的通道键区分，保证每一侧的配额集合恒定。
                if quota.always_budgets and resource:
                    add(("relationship", matched, resource), quota.always_budgets)
                if self._is_proactive(quota, json_body):
                    budget_kind = _QUOTA_KINDS.get(matched)
                    # 规则里显式写明的 Bot 维度配额优先；否则按认证等级取默认档位。
                    bot_budgets = quota.bot_budgets
                    if not bot_budgets and budget_kind:
                        bot_budgets = PROACTIVE_BOT_BUDGETS[self.certification].get(budget_kind, ())
                    add(("bot", "proactive", budget_kind or matched), bot_budgets)
                    if quota.relationship_budgets and resource:
                        add(("proactive", matched, resource), quota.relationship_budgets)

        # 固定顺序加锁，避免同一请求持有多把通道锁时与其它请求交叉死锁。
        return sorted(planned.items(), key=lambda item: item[0])

    @staticmethod
    def _is_proactive(quota: MessageQuota, json_body: Any) -> bool:
        """没有 ``msg_id`` / ``event_id`` 的消息即为主动消息。"""

        if not quota.bot_budgets and not quota.relationship_budgets:
            return False
        if not isinstance(json_body, Mapping):
            return True
        return not any(json_body.get(marker) for marker in _PASSIVE_MARKERS)

    @staticmethod
    def _resource_id(matched: str, path: str) -> Optional[str]:
        """取路由模板中第一个参数在具体路径上的取值，作为「单关系」标识。"""

        _method, _, template = matched.partition(" ")
        template_segments = _segments(template)
        actual_segments = _segments(path)
        if len(template_segments) != len(actual_segments):
            return None
        for template_segment, actual_segment in zip(template_segments, actual_segments):
            if template_segment == "{}":
                return actual_segment
        return None

    def _channel(self, key: Tuple[Any, ...], budgets: Sequence[RateLimitBudget]) -> _Channel:
        channel = self._channels.get(key)
        if channel is not None and channel.budgets == tuple(budgets):
            self._channels.move_to_end(key)
            return channel
        channel = _Channel(budgets, self._clock)
        self._channels[key] = channel
        self._channels.move_to_end(key)
        self._evict()
        return channel

    def _evict(self) -> None:
        if len(self._channels) <= self.max_buckets:
            return
        # 最近使用的通道永远位于末尾；只从更旧的条目里回收，且跳过正在排队或
        # 持有锁的通道，避免把仍在使用的令牌桶丢掉。
        for key in list(self._channels.keys())[:-1]:
            if len(self._channels) <= self.max_buckets:
                break
            channel = self._channels[key]
            if channel.lock.locked() or channel.pending:
                continue
            del self._channels[key]

    async def _acquire_channel(self, key: Tuple[Any, ...], budgets: Sequence[RateLimitBudget]) -> None:
        channel = self._channel(key, budgets)
        channel.pending += 1
        try:
            # 每个通道一把锁：等待者按到达顺序排队，锁内只做「等令牌 + 取令牌」，
            # 因此同一通道的请求天然被整形成均匀间隔。
            async with channel.lock:
                while True:
                    now = self._clock()
                    delay = 0.0
                    for bucket in channel.buckets:
                        waiting = bucket.delay(now)
                        if waiting <= 0:
                            continue
                        if bucket.budget.window > self.max_pacing_window:
                            # 每日上限属于「配额」而非速率：把它当作硬等待会让请求
                            # 挂起数小时。这里只提示，仍然放行，让平台返回结构化错误。
                            if not bucket.warned:
                                bucket.warned = True
                                self._logger.warning(
                                    "[botpy] %s 的 %s 配额已用尽，本次请求仍会发出，可能被平台拒绝",
                                    " ".join(str(part) for part in key),
                                    bucket.budget.describe(),
                                )
                            continue
                        delay = max(delay, waiting)
                    if delay <= 0:
                        for bucket in channel.buckets:
                            bucket.consume(now)
                        return
                    if self._logger.isEnabledFor(logging.DEBUG):
                        self._logger.debug(
                            "[botpy] 整流排队 %s：等待 %.3fs",
                            " ".join(str(part) for part in key),
                            delay,
                        )
                    await self._sleep(delay)
        finally:
            channel.pending -= 1


def build_limiter(
    value: Any = None,
    *,
    enabled: bool = True,
) -> Optional[RateLimiter]:
    """把 ``Client(rate_limit=...)`` 的取值规整成 :class:`RateLimiter` 或 ``None``。"""

    if value is None or value is True:
        return RateLimiter(enabled=enabled) if enabled else None
    if value is False:
        return None
    if isinstance(value, RateLimiter):
        return value
    if isinstance(value, Mapping):
        options = dict(value)
        options.setdefault("enabled", enabled)
        return RateLimiter(**options)
    raise TypeError("rate_limit must be a bool, a mapping of RateLimiter options, or a RateLimiter instance")
