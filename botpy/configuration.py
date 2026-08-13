# -*- coding: utf-8 -*-
"""声明式菜单、指令面板及其远端同步。"""

import asyncio
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .api import BotAPI, _validate_menu, _validate_panel, _validate_panel_scope
from .protocol.errors import BotPyError


_PANEL_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PANEL_MARKER_RE = re.compile(r"^\[botpy:([A-Za-z0-9._-]{1,64})\](?:\s|$)")


class ConfigurationSyncError(BotPyError):
    """声明式配置无法安全地与远端状态协调。"""


@dataclass(frozen=True)
class ConfigurationSyncResult:
    menu_changed: bool
    panels_created: int
    panels_updated: int
    panel_targets_changed: int


class _MenuItemFactory:
    """一级和二级菜单共用的声明式项工厂。"""

    @staticmethod
    def message(name: str, content: str) -> Dict[str, Any]:
        return {"type": "send_message", "name": name, "send_message": content}

    @staticmethod
    def link(name: str, url: str) -> Dict[str, Any]:
        return {"type": "link", "name": name, "link": url}


class Menu:
    """一份 C2C 全局菜单声明。"""

    def __init__(self, *, items: Sequence[Mapping[str, Any]]):
        data = _validate_menu({"items": deepcopy(list(items))})
        self._validate_declared_items(data["items"])
        self._data = data

    @staticmethod
    def _validate_declared_items(items: Sequence[Mapping[str, Any]]) -> None:
        allowed = {
            "send_message": {"type", "name", "send_message"},
            "link": {"type", "name", "link"},
            "switch": {"type", "name", "switch"},
            "menu": {"type", "name", "sub_menu_items"},
        }
        for item in items:
            extra = set(item) - allowed[item["type"]]
            if extra:
                raise ValueError(f"menu item contains fields invalid for type {item['type']!r}: {sorted(extra)}")
            if item["type"] == "menu":
                for sub_item in item["sub_menu_items"]:
                    sub_allowed = {"type", "name", sub_item["type"]}
                    extra = set(sub_item) - sub_allowed
                    if extra:
                        raise ValueError(
                            f"submenu item contains fields invalid for type {sub_item['type']!r}: {sorted(extra)}"
                        )

    message = staticmethod(_MenuItemFactory.message)
    link = staticmethod(_MenuItemFactory.link)
    sub = _MenuItemFactory()

    @staticmethod
    def switch(name: str, switch_id: str, *, default: bool = False) -> Dict[str, Any]:
        return {
            "type": "switch",
            "name": name,
            "switch": {"switch_id": switch_id, "default": default},
        }

    @staticmethod
    def submenu(name: str, items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        return {"type": "menu", "name": name, "sub_menu_items": deepcopy(list(items))}

    @classmethod
    def empty(cls) -> "Menu":
        """显式声明清空远端菜单。"""

        return cls(items=[])

    def to_dict(self) -> Dict[str, Any]:
        return deepcopy(self._data)


class Panel:
    """一块由 SDK 使用稳定 key 管理的指令面板声明。"""

    def __init__(
        self,
        key: str,
        *,
        scope: str,
        items: Sequence[Mapping[str, Any]],
        target_type: str = "all",
        user_openids: Optional[Sequence[str]] = None,
        group_openids: Optional[Sequence[str]] = None,
        remark: Optional[str] = None,
    ):
        if not isinstance(key, str) or not _PANEL_KEY_RE.fullmatch(key):
            raise ValueError("panel key must contain 1-64 letters, digits, '.', '_', or '-'")
        _validate_panel_scope(scope)
        if target_type not in {"all", "specific"}:
            raise ValueError("target_type must be 'all' or 'specific'")
        if not isinstance(remark, (str, type(None))):
            raise TypeError("remark must be a string or None")

        users = self._validate_targets(user_openids, "user_openids")
        groups = self._validate_targets(group_openids, "group_openids")
        if target_type == "all":
            if users is not None or groups is not None:
                raise ValueError("target lists are not supported when target_type='all'")
        elif scope == "c2c":
            if not users or groups is not None:
                raise ValueError("specific c2c panels require only user_openids")
        elif scope == "group":
            if not groups or users is not None:
                raise ValueError("specific group panels require only group_openids")
        else:
            raise ValueError("specific panels are only supported for c2c and group scopes")

        marker = f"[botpy:{key}]"
        managed_remark = marker if not remark else f"{marker} {remark}"
        if len(managed_remark) > 255:
            raise ValueError("remark and the managed panel key must not exceed 255 characters")
        panel_data = _validate_panel({"items": deepcopy(list(items)), "remark": managed_remark})
        for item in panel_data["items"]:
            allowed = {"type", "name", "desc", "only_admin"}
            if item["type"] == "link":
                allowed.add("link")
            extra = set(item) - allowed
            if extra:
                raise ValueError(f"panel item contains fields invalid for type {item['type']!r}: {sorted(extra)}")
        self.key = key
        self.scope = scope
        self.target_type = target_type
        self.user_openids = tuple(users or ())
        self.group_openids = tuple(groups or ())
        self.remark = remark
        self._data = panel_data

    @staticmethod
    def _validate_targets(values: Optional[Sequence[str]], name: str) -> Optional[List[str]]:
        if values is None:
            return None
        if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
            raise TypeError(f"{name} must be a sequence")
        result = list(values)
        if not result:
            raise ValueError(f"{name} must not be empty")
        if len(result) > 1000:
            raise ValueError(f"{name} supports at most 1000 items")
        if any(not isinstance(value, str) or not value for value in result):
            raise ValueError(f"{name} must contain non-empty strings")
        if len(set(result)) != len(result):
            raise ValueError(f"{name} must not contain duplicates")
        return result

    @staticmethod
    def command(name: str, *, desc: Optional[str] = None, only_admin: bool = False) -> Dict[str, Any]:
        item: Dict[str, Any] = {"type": "command", "name": name, "only_admin": only_admin}
        if desc is not None:
            item["desc"] = desc
        return item

    @staticmethod
    def link(
        name: str,
        url: str,
        *,
        desc: Optional[str] = None,
        only_admin: bool = False,
    ) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "type": "link",
            "name": name,
            "link": url,
            "only_admin": only_admin,
        }
        if desc is not None:
            item["desc"] = desc
        return item

    def to_dict(self) -> Dict[str, Any]:
        return deepcopy(self._data)


def _panel_key(record: Mapping[str, Any]) -> Optional[str]:
    panel = record.get("panel")
    if not isinstance(panel, Mapping):
        return None
    remark = panel.get("remark")
    if not isinstance(remark, str):
        return None
    match = _PANEL_MARKER_RE.match(remark)
    return match.group(1) if match else None


def _canonical_menu(value: Any) -> Dict[str, Any]:
    if value is None:
        return {"items": []}
    if not isinstance(value, Mapping):
        raise ConfigurationSyncError("remote menu response is malformed")
    items = value.get("items", [])
    if not isinstance(items, list):
        raise ConfigurationSyncError("remote menu items are malformed")
    result = []
    for raw in items:
        if not isinstance(raw, Mapping):
            raise ConfigurationSyncError("remote menu item is malformed")
        item_type = raw.get("type")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigurationSyncError("remote menu item name is malformed")
        item = {"type": item_type, "name": name}
        if item_type == "send_message":
            content = raw.get("send_message")
            if not isinstance(content, str) or not content:
                raise ConfigurationSyncError("remote send_message item is malformed")
            item["send_message"] = content
        elif item_type == "link":
            link = raw.get("link")
            if not isinstance(link, str) or not link:
                raise ConfigurationSyncError("remote link menu item is malformed")
            item["link"] = link
        elif item_type == "switch":
            switch = raw.get("switch")
            if (
                not isinstance(switch, Mapping)
                or not isinstance(switch.get("switch_id"), str)
                or not switch.get("switch_id")
                or not isinstance(switch.get("default"), bool)
            ):
                raise ConfigurationSyncError("remote switch menu item is malformed")
            item["switch"] = {
                "switch_id": switch["switch_id"],
                "default": switch["default"],
            }
        elif item_type == "menu":
            sub_items = raw.get("sub_menu_items", [])
            if not isinstance(sub_items, list) or any(not isinstance(sub, Mapping) for sub in sub_items):
                raise ConfigurationSyncError("remote submenu items are malformed")
            item["sub_menu_items"] = [_canonical_sub_menu_item(sub) for sub in sub_items]
        else:
            raise ConfigurationSyncError("remote menu item type is malformed")
        result.append(item)
    return {"items": result}


def _canonical_sub_menu_item(value: Mapping[str, Any]) -> Dict[str, Any]:
    item_type = value.get("type")
    name = value.get("name")
    if not isinstance(name, str) or not name or item_type not in {"send_message", "link"}:
        raise ConfigurationSyncError("remote submenu item is malformed")
    result = {"type": item_type, "name": name}
    field = "send_message" if item_type == "send_message" else "link"
    content = value.get(field)
    if not isinstance(content, str) or not content:
        raise ConfigurationSyncError("remote submenu item content is malformed")
    result[field] = content
    return result


def _canonical_panel(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationSyncError("remote panel response is malformed")
    items = value.get("items", [])
    if not isinstance(items, list):
        raise ConfigurationSyncError("remote panel items are malformed")
    result_items = []
    for raw in items:
        if not isinstance(raw, Mapping):
            raise ConfigurationSyncError("remote panel item is malformed")
        item_type = raw.get("type")
        name = raw.get("name")
        if item_type not in {"command", "link"} or not isinstance(name, str) or not name:
            raise ConfigurationSyncError("remote panel item is malformed")
        item = {
            "type": item_type,
            "name": name,
            "only_admin": raw.get("only_admin", False),
        }
        if not isinstance(item["only_admin"], bool):
            raise ConfigurationSyncError("remote panel only_admin is malformed")
        if "desc" in raw:
            if not isinstance(raw.get("desc"), str):
                raise ConfigurationSyncError("remote panel desc is malformed")
            item["desc"] = raw["desc"]
        if item_type == "link":
            if not isinstance(raw.get("link"), str) or not raw.get("link"):
                raise ConfigurationSyncError("remote panel link is malformed")
            item["link"] = raw["link"]
        result_items.append(item)
    return {"items": result_items, "remark": value.get("remark", "")}


def _chunks(values: Sequence[str], size: int = 20) -> Iterable[List[str]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


class ConfigurationManager:
    """将 Client 声明的 Menu 和 Panel 非破坏性同步到平台。"""

    def __init__(self, api: BotAPI, *, menu: Optional[Menu] = None, panels: Sequence[Panel] = ()):
        if menu is not None and not isinstance(menu, Menu):
            raise TypeError("menu must be a Menu or None")
        panel_list = tuple(panels)
        if any(not isinstance(panel, Panel) for panel in panel_list):
            raise TypeError("panels must contain Panel instances")
        if len(panel_list) > 20:
            raise ValueError("a bot supports at most 20 declared panels")
        keys = [panel.key for panel in panel_list]
        if len(set(keys)) != len(keys):
            raise ValueError("panel keys must be unique")
        self.api = api
        self.menu = menu
        self.panels = panel_list
        self.menu_snapshot: Optional[Dict[str, Any]] = None
        self.menu_version: Optional[int] = None
        self.panel_snapshots: Dict[str, Dict[str, Any]] = {}
        self.last_error: Optional[BaseException] = None
        self._task_lock: Optional[asyncio.Lock] = None
        self._sync_task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return self.menu is not None or bool(self.panels)

    def _get_task_lock(self) -> asyncio.Lock:
        if self._task_lock is None:
            self._task_lock = asyncio.Lock()
        return self._task_lock

    async def sync(self) -> ConfigurationSyncResult:
        """同步全部声明；同一进程中的并发调用共享同一个同步任务。"""

        lock = self._get_task_lock()
        async with lock:
            task = self._sync_task
            if task is None or task.done():
                task = asyncio.create_task(self._sync())
                self._sync_task = task
        try:
            return await asyncio.shield(task)
        except BaseException as exc:
            self.last_error = exc
            raise
        finally:
            if task.done():
                async with lock:
                    if self._sync_task is task:
                        self._sync_task = None

    async def close(self) -> None:
        """取消并等待仍在运行的配置同步。"""

        lock = self._get_task_lock()
        async with lock:
            task = self._sync_task
            if task is None or task.done():
                return
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _sync(self) -> ConfigurationSyncResult:
        menu_changed = await self._sync_menu() if self.menu is not None else False
        created, updated, targets = await self._sync_panels() if self.panels else (0, 0, 0)
        self.last_error = None
        return ConfigurationSyncResult(menu_changed, created, updated, targets)

    async def _sync_menu(self) -> bool:
        response = await self.api.get_menu()
        if not isinstance(response, Mapping):
            raise ConfigurationSyncError("remote menu response is malformed")
        remote = _canonical_menu(response.get("menu"))
        desired = _canonical_menu(self.menu.to_dict())
        self.menu_snapshot = deepcopy(remote)
        self.menu_version = response.get("version") if isinstance(response.get("version"), int) else None
        if remote == desired:
            return False
        updated = await self.api.update_menu(self.menu.to_dict())
        self.menu_snapshot = deepcopy(desired)
        if isinstance(updated, Mapping) and "version" in updated:
            self.menu_version = updated["version"]
        return True

    async def _fetch_panels(self) -> List[Mapping[str, Any]]:
        records: List[Mapping[str, Any]] = []
        # key 在机器人维度唯一；读取全部场景才能发现被错误移动或重复的受管面板。
        for scope in ("c2c", "group", "channel", "dm"):
            cursor: Optional[str] = None
            seen = set()
            while True:
                response = await self.api.get_panels(scope, cursor=cursor, limit=50)
                if not isinstance(response, Mapping):
                    raise ConfigurationSyncError("remote panel list response is malformed")
                is_end = response.get("is_end")
                if not isinstance(is_end, bool):
                    raise ConfigurationSyncError("remote panel list is_end is malformed")
                page_records = response.get("records", [])
                if not isinstance(page_records, list):
                    raise ConfigurationSyncError("remote panel list records are malformed")
                if any(not isinstance(record, Mapping) for record in page_records):
                    raise ConfigurationSyncError("remote panel record is malformed")
                records.extend(page_records)
                if is_end:
                    break
                next_cursor = response.get("next_cursor")
                if not isinstance(next_cursor, str) or next_cursor in seen:
                    raise ConfigurationSyncError("remote panel cursor is malformed")
                if not next_cursor:
                    raise ConfigurationSyncError("remote panel cursor is missing before the final page")
                seen.add(next_cursor)
                cursor = next_cursor
        return records

    async def _sync_panels(self) -> Tuple[int, int, int]:
        records = await self._fetch_panels()
        managed: Dict[str, List[Mapping[str, Any]]] = {}
        for record in records:
            key = _panel_key(record)
            if key is not None:
                managed.setdefault(key, []).append(record)

        plans = []
        missing = 0
        for declaration in self.panels:
            matches = managed.get(declaration.key, [])
            if len(matches) > 1:
                raise ConfigurationSyncError(f"multiple remote panels use managed key {declaration.key!r}")
            if not matches:
                missing += 1
                plans.append((declaration, None, False, ()))
                continue

            record = matches[0]
            panel_id = record.get("panel_id")
            if not isinstance(panel_id, str) or not panel_id:
                raise ConfigurationSyncError("managed panel is missing panel_id")
            if record.get("scope") != declaration.scope or record.get("target_type", "all") != declaration.target_type:
                raise ConfigurationSyncError(
                    f"managed panel {declaration.key!r} cannot change scope or target_type in place"
                )
            desired_panel = _canonical_panel(declaration.to_dict())
            remote_panel = _canonical_panel(record.get("panel"))
            target_changes = ()
            if declaration.target_type == "specific":
                target_changes = await self._plan_panel_targets(panel_id, declaration)
            plans.append((declaration, panel_id, remote_panel != desired_panel, target_changes))

        panel_ids = []
        for record in records:
            panel_id = record.get("panel_id")
            if not isinstance(panel_id, str) or not panel_id:
                raise ConfigurationSyncError("remote panel record is missing panel_id")
            panel_ids.append(panel_id)
        if len(set(panel_ids)) != len(panel_ids):
            raise ConfigurationSyncError("remote panel list contains duplicate panel_id values")
        if len(panel_ids) + missing > 20:
            raise ConfigurationSyncError(
                "declared panels would exceed the platform limit of 20; remove remote panels first"
            )

        created = updated = targets_changed = 0
        for declaration, panel_id, needs_update, target_changes in plans:
            if panel_id is None:
                panel_id = await self._create_panel(declaration)
                created += 1
            elif needs_update:
                await self.api.update_panel(panel_id, declaration.to_dict())
                updated += 1
            for op, field, values in target_changes:
                for batch in _chunks(values):
                    await self.api.update_panel_targets(panel_id, op=op, **{field: batch})
                    targets_changed += len(batch)
            self.panel_snapshots[declaration.key] = {
                "panel_id": panel_id,
                "panel": declaration.to_dict(),
            }
        return created, updated, targets_changed

    async def _create_panel(self, declaration: Panel) -> str:
        users = list(declaration.user_openids)
        groups = list(declaration.group_openids)
        response = await self.api.create_panel(
            declaration.scope,
            declaration.to_dict(),
            target_type=declaration.target_type,
            user_openids=users[:20] or None,
            group_openids=groups[:20] or None,
        )
        if not isinstance(response, Mapping) or not isinstance(response.get("panel_id"), str):
            raise ConfigurationSyncError("create panel response is missing panel_id")
        panel_id = response["panel_id"]
        for batch in _chunks(users[20:]):
            await self.api.update_panel_targets(panel_id, op="add", user_openids=batch)
        for batch in _chunks(groups[20:]):
            await self.api.update_panel_targets(panel_id, op="add", group_openids=batch)
        return panel_id

    async def _plan_panel_targets(
        self,
        panel_id: str,
        declaration: Panel,
    ) -> Tuple[Tuple[str, str, List[str]], ...]:
        detail = await self.api.get_panel(panel_id)
        if not isinstance(detail, Mapping):
            raise ConfigurationSyncError("remote panel detail response is malformed")
        field = "user_openids" if declaration.scope == "c2c" else "group_openids"
        desired = set(getattr(declaration, field))
        raw_current = detail.get(field, [])
        if not isinstance(raw_current, list) or any(not isinstance(value, str) for value in raw_current):
            raise ConfigurationSyncError(f"remote {field} response is malformed")
        current = set(raw_current)
        return tuple(
            (op, field, values)
            for op, values in (("del", sorted(current - desired)), ("add", sorted(desired - current)))
            if values
        )


__all__ = (
    "ConfigurationManager",
    "ConfigurationSyncError",
    "ConfigurationSyncResult",
    "Menu",
    "Panel",
)
