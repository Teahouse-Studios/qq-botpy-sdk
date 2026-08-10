import inspect
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Optional, Union

from .base import Middleware, MiddlewareContext


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    args: tuple[str, ...]
    raw: str


CommandResult = Union[str, dict[str, Any], None]
CommandScope = Literal["all", "c2c", "group", "channel", "dm"]


@dataclass(frozen=True)
class SlashCommand:
    name: Union[str, tuple[str, ...]]
    handler: Callable[[MiddlewareContext], Any]
    description: str = ""
    usage: str = ""
    hidden: bool = False
    scope: CommandScope = "all"
    authorized: Optional[Callable[[MiddlewareContext], Any]] = None


class SlashCommandRouter:
    def __init__(
        self,
        *,
        prefixes: Iterable[str] = ("/",),
        commands: Iterable[SlashCommand] = (),
        catch_errors: bool = True,
        auto_help: bool = True,
        allow_from: Iterable[str] = (),
        require_mention_in_group: bool = True,
        on_result: Optional[Callable[[MiddlewareContext, CommandResult], Any]] = None,
    ) -> None:
        self.prefixes = tuple(prefixes)
        if not self.prefixes or any(not prefix for prefix in self.prefixes):
            raise ValueError("slash command prefixes must not be empty")
        self.catch_errors = catch_errors
        self.allow_from = frozenset(allow_from)
        self.require_mention_in_group = require_mention_in_group
        self.on_result = on_result
        self._registry: dict[str, SlashCommand] = {}
        for command in commands:
            self.register(command)
        if auto_help and "help" not in self._registry:
            self.register(
                SlashCommand(
                    name="help",
                    description="列出所有可用命令",
                    handler=lambda context: self._help_text(),
                )
            )

    def register(self, command: SlashCommand) -> None:
        names = (command.name,) if isinstance(command.name, str) else command.name
        if not names or any(not name.strip() for name in names):
            raise ValueError("slash command name must not be empty")
        for name in names:
            key = name.lower()
            if key in self._registry:
                raise ValueError(f'duplicate slash command name "{name}"')
        for name in names:
            self._registry[name.lower()] = command

    def unregister(self, name: str) -> None:
        self._registry.pop(name.lower(), None)

    def list(self) -> tuple[SlashCommand, ...]:
        commands = []
        seen = set()
        for command in self._registry.values():
            marker = id(command)
            if marker not in seen:
                seen.add(marker)
                commands.append(command)
        return tuple(commands)

    async def middleware(self, context: MiddlewareContext, next_call) -> None:
        content = (context.message.content or "").strip()
        if not content:
            await next_call()
            return
        cleaned = re.sub(r"<@!?[^>]+>\s*", "", content).strip()

        if self.require_mention_in_group and context.reply_target.scope == "group":
            mention = context.state.get("mention")
            was_mentioned = getattr(mention, "was_mentioned", False)
            was_mentioned = was_mentioned or context.message.event_type == "GROUP_AT_MESSAGE_CREATE"
            mentions = context.message.metadata.get("mentions")
            was_mentioned = was_mentioned or (
                isinstance(mentions, list)
                and any(isinstance(item, dict) and item.get("is_you") is True for item in mentions)
            )
            if not was_mentioned:
                await next_call()
                return

        prefix = next((item for item in self.prefixes if cleaned.startswith(item)), None)
        if prefix is None:
            await next_call()
            return
        match = re.fullmatch(r"(\S+)(?:\s+(.*))?", cleaned[len(prefix) :])
        if match is None:
            await next_call()
            return

        name = match.group(1).lower()
        command = self._registry.get(name)
        if command is None:
            await next_call()
            return
        sender_id = context.message.author_id or ""
        if self.allow_from and "*" not in self.allow_from and sender_id not in self.allow_from:
            await next_call()
            return

        raw = match.group(2) or ""
        parsed = ParsedCommand(name=name, args=tuple(raw.split()) if raw else (), raw=raw)
        context.state["command"] = parsed
        context.command = parsed

        if not _scope_matches(command.scope, context.reply_target.scope):
            label = "私聊" if command.scope in ("c2c", "dm") else "群聊或频道"
            await self._send_result(context, f"该指令仅限{label}使用")
            context.stop(f"command:scope-denied:{name}")
            return

        if command.authorized is not None:
            authorization = await _maybe_await(command.authorized(context))
            if authorization is not True:
                result = authorization if isinstance(authorization, str) else "⚠️ 无权限执行此命令"
                await self._send_result(context, result)
                context.stop(f"command:unauthorized:{name}")
                return

        try:
            result = await _maybe_await(command.handler(context))
            await self._send_result(context, result)
        except Exception as exc:
            if not self.catch_errors:
                context.stop(f"command:error:{name}")
                raise
            context.log.error('[botpy] Slash Command "%s" 执行失败: %s', name, exc)
            await self._send_result(context, f"Error: {exc}")
        context.stop(f"command:matched:{name}")

    async def _send_result(self, context: MiddlewareContext, result: CommandResult) -> None:
        if result is None:
            return
        if self.on_result is not None:
            await _maybe_await(self.on_result(context, result))
            return
        if isinstance(result, str):
            content = result
        elif isinstance(result, dict) and result.get("kind") == "text":
            content = str(result.get("content") or "")
        else:
            return
        if content:
            await context.client.send_text(context.reply_target, content)

    def _help_text(self) -> str:
        lines = ["可用命令："]
        for command in self.list():
            if command.hidden:
                continue
            names = (command.name,) if isinstance(command.name, str) else command.name
            usage = f" — {command.usage}" if command.usage else ""
            description = f" — {command.description}" if command.description else ""
            lines.append(f"/{', '.join(names)}{usage}{description}")
        return "\n".join(lines)


def slash_command(**options: Any) -> SlashCommandRouter:
    return SlashCommandRouter(**options)


def _scope_matches(command_scope: CommandScope, message_scope: str) -> bool:
    if command_scope == "all":
        return True
    if command_scope == "c2c":
        return message_scope in ("c2c", "dm")
    return command_scope == message_scope


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
