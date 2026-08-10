import inspect
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Literal, Mapping, Optional, Pattern, Union

from .base import Middleware, MiddlewareContext, resolve_policy


AccessMatcher = Union[
    str,
    Pattern[str],
    Callable[[MiddlewareContext], Union[bool, Awaitable[bool]]],
]
AccessMode = Literal["open", "allowlist", "disabled"]


@dataclass(frozen=True)
class ScopePolicy:
    mode: AccessMode = "open"
    allow: tuple[AccessMatcher, ...] = ()
    deny: tuple[AccessMatcher, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in ("open", "allowlist", "disabled"):
            raise ValueError("scope policy mode must be 'open', 'allowlist', or 'disabled'")
        object.__setattr__(self, "allow", _coerce_matchers(self.allow))
        object.__setattr__(self, "deny", _coerce_matchers(self.deny))


@dataclass(frozen=True)
class MentionDecision:
    was_mentioned: bool
    implicit: bool
    should_answer: bool
    reason: Literal["no_mention", "other_mention", "passthrough"]


def access_policy(
    *,
    c2c: Optional[Union[ScopePolicy, Mapping[str, Any]]] = None,
    group: Optional[Union[ScopePolicy, Mapping[str, Any]]] = None,
    guild: Optional[Union[ScopePolicy, Mapping[str, Any]]] = None,
    on_block: Optional[Callable[[MiddlewareContext, str], Any]] = None,
) -> Middleware:
    """按私聊、群聊和频道作用域执行 allow/deny 访问策略。"""

    policies = {
        "c2c": _coerce_scope_policy(c2c),
        "group": _coerce_scope_policy(group),
        "guild": _coerce_scope_policy(guild),
    }

    async def middleware(context: MiddlewareContext, next_call) -> None:
        scope = context.reply_target.scope
        if scope in ("c2c", "dm"):
            policy = policies["c2c"]
            identifier = context.message.author_id or context.reply_target.target_id
        elif scope == "group":
            policy = policies["group"]
            identifier = context.reply_target.target_id
        elif scope == "channel":
            policy = policies["guild"]
            identifier = context.reply_target.target_id
        else:
            await next_call()
            return

        allowed, reason = await _evaluate_scope(policy, identifier, context)
        if not allowed:
            if on_block is not None:
                await _maybe_await(on_block(context, reason))
            context.stop(f"access:{reason}")
            return
        await next_call()

    return middleware


def mention_gate(
    *,
    require_mention_in_group: bool = True,
    always_answer_c2c: bool = True,
    is_implicit_mention: Optional[Callable[[MiddlewareContext], Union[bool, Awaitable[bool]]]] = None,
    on_skip: Optional[Callable[[MiddlewareContext, MentionDecision], Any]] = None,
    ignore_other_mentions: bool = False,
    passthrough: bool = False,
    resolve_config: Optional[Callable[[MiddlewareContext], Mapping[str, bool]]] = None,
) -> Middleware:
    """判断群消息是否明确提及机器人，并将决定写入 ``state['mention']``。"""

    async def middleware(context: MiddlewareContext, next_call) -> None:
        scope = context.reply_target.scope
        if scope != "group":
            should_answer = always_answer_c2c or scope not in ("c2c", "dm")
            decision = MentionDecision(True, False, should_answer, "passthrough")
            context.state["mention"] = decision
            if not should_answer and not passthrough:
                await _notify_skip(on_skip, context, decision)
                context.stop("mention-gate:passthrough")
                return
            await next_call()
            return

        dynamic = (resolve_config(context) or {}) if resolve_config is not None else {}
        effective_required = resolve_policy(
            context,
            "group.require_mention",
            dynamic.get("require_mention_in_group"),
            require_mention_in_group,
        )
        effective_ignore_other = resolve_policy(
            context,
            "group.ignore_other_mentions",
            dynamic.get("ignore_other_mentions"),
            ignore_other_mentions,
        )

        mentions = context.message.metadata.get("mentions")
        structured_mention = isinstance(mentions, list) and any(
            isinstance(item, Mapping) and item.get("is_you") is True for item in mentions
        )
        app_id = str(getattr(context.client, "_appid", "") or "")
        content_mention = bool(
            app_id and re.search(rf"<@!?{re.escape(app_id)}>", context.message.content or "")
        )
        was_mentioned = (
            context.message.event_type == "GROUP_AT_MESSAGE_CREATE"
            or structured_mention
            or content_mention
        )
        implicit = False
        if is_implicit_mention is not None:
            implicit = bool(await _maybe_await(is_implicit_mention(context)))

        should_answer = not effective_required or was_mentioned or implicit
        reason: Literal["no_mention", "other_mention", "passthrough"]
        reason = "passthrough" if should_answer else "no_mention"
        if effective_ignore_other and effective_required and not was_mentioned and not implicit:
            if isinstance(mentions, list) and mentions:
                reason = "other_mention"

        decision = MentionDecision(was_mentioned, implicit, should_answer, reason)
        context.state["mention"] = decision
        if not should_answer:
            await _notify_skip(on_skip, context, decision)
            if not passthrough:
                context.stop(f"mention-gate:{reason}")
                return
        await next_call()

    return middleware


def _coerce_scope_policy(value: Optional[Union[ScopePolicy, Mapping[str, Any]]]) -> ScopePolicy:
    if value is None:
        return ScopePolicy()
    if isinstance(value, ScopePolicy):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("scope policy must be ScopePolicy or a mapping")
    return ScopePolicy(
        mode=value.get("mode", "open"),
        allow=value.get("allow") or (),
        deny=value.get("deny") or (),
    )


def _coerce_matchers(value: Any) -> tuple[AccessMatcher, ...]:
    if isinstance(value, (str, re.Pattern)) or callable(value):
        return (value,)
    return tuple(value or ())


async def _evaluate_scope(
    policy: ScopePolicy,
    identifier: str,
    context: MiddlewareContext,
) -> tuple[bool, str]:
    if await _matches_any(policy.deny, identifier, context):
        return False, f"denied by deny-list ({identifier})"
    if policy.mode == "disabled":
        return False, "scope disabled by policy"
    if policy.mode == "open":
        return True, ""
    if not policy.allow:
        return False, "allowlist is empty"
    if await _matches_any(policy.allow, identifier, context):
        return True, ""
    return False, f"not in allowlist ({identifier})"


async def _matches_any(
    matchers: Iterable[AccessMatcher],
    identifier: str,
    context: MiddlewareContext,
) -> bool:
    for matcher in matchers:
        if matcher == "*":
            return True
        if isinstance(matcher, str) and matcher == identifier:
            return True
        if isinstance(matcher, re.Pattern) and matcher.search(identifier):
            return True
        if callable(matcher) and bool(await _maybe_await(matcher(context))):
            return True
    return False


async def _notify_skip(callback, context, decision) -> None:
    if callback is not None:
        await _maybe_await(callback(context, decision))


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
