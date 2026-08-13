import asyncio
import unittest

from botpy.client import Client
from botpy.configuration import ConfigurationManager, ConfigurationSyncError, Menu, Panel


class FakeApi:
    def __init__(self):
        self.menu = None
        self.menu_version = 0
        self.records = []
        self.details = {}
        self.calls = []
        self._next_panel = 1

    async def get_menu(self):
        self.calls.append(("get_menu",))
        return {"menu": self.menu, "version": self.menu_version}

    async def update_menu(self, menu):
        self.calls.append(("update_menu", menu))
        self.menu = menu
        self.menu_version += 1
        return {"version": self.menu_version}

    async def get_panels(self, scope, *, cursor=None, limit=20):
        self.calls.append(("get_panels", scope, cursor, limit))
        return {
            "records": [record for record in self.records if record["scope"] == scope],
            "next_cursor": "",
            "is_end": True,
        }

    async def create_panel(
        self,
        scope,
        panel,
        *,
        target_type="all",
        user_openids=None,
        group_openids=None,
    ):
        panel_id = f"panel-{self._next_panel}"
        self._next_panel += 1
        self.calls.append(
            (
                "create_panel",
                scope,
                panel,
                target_type,
                user_openids,
                group_openids,
            )
        )
        record = {
            "panel_id": panel_id,
            "scope": scope,
            "target_type": target_type,
            "panel": panel,
        }
        self.records.append(record)
        self.details[panel_id] = {
            **record,
            "user_openids": list(user_openids or ()),
            "group_openids": list(group_openids or ()),
        }
        return {"panel_id": panel_id}

    async def update_panel(self, panel_id, panel):
        self.calls.append(("update_panel", panel_id, panel))
        for record in self.records:
            if record["panel_id"] == panel_id:
                record["panel"] = panel
        self.details[panel_id]["panel"] = panel
        return {"version": 2}

    async def get_panel(self, panel_id):
        self.calls.append(("get_panel", panel_id))
        return self.details[panel_id]

    async def update_panel_targets(self, panel_id, *, op, user_openids=None, group_openids=None):
        field = "user_openids" if user_openids is not None else "group_openids"
        values = list(user_openids if user_openids is not None else group_openids)
        self.calls.append(("update_panel_targets", panel_id, op, field, values))
        targets = self.details[panel_id][field]
        if op == "add":
            targets.extend(value for value in values if value not in targets)
        else:
            self.details[panel_id][field] = [value for value in targets if value not in values]
        return None


class DeclarativeConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_is_only_updated_when_content_differs(self):
        api = FakeApi()
        menu = Menu(items=[Menu.message("帮助", "/help")])
        api.menu = menu.to_dict()
        manager = ConfigurationManager(api, menu=menu)

        unchanged = await manager.sync()
        self.assertFalse(unchanged.menu_changed)
        self.assertFalse(any(call[0] == "update_menu" for call in api.calls))

        api.menu = None
        changed = await manager.sync()
        self.assertTrue(changed.menu_changed)
        self.assertEqual(menu.to_dict(), api.menu)

    async def test_explicit_empty_menu_clears_remote_menu(self):
        api = FakeApi()
        api.menu = Menu(items=[Menu.message("帮助", "/help")]).to_dict()

        result = await ConfigurationManager(api, menu=Menu.empty()).sync()

        self.assertTrue(result.menu_changed)
        self.assertEqual({"items": []}, api.menu)

    def test_submenu_uses_dedicated_subitem_factory(self):
        menu = Menu(
            items=[
                Menu.submenu(
                    "更多",
                    [Menu.sub.message("设置", "/settings"), Menu.sub.link("文档", "https://example.com")],
                )
            ]
        )

        self.assertEqual("send_message", menu.to_dict()["items"][0]["sub_menu_items"][0]["type"])

    async def test_panel_creation_uses_managed_key_and_batches_targets(self):
        api = FakeApi()
        panel = Panel(
            "weather",
            scope="c2c",
            target_type="specific",
            user_openids=[f"user-{index}" for index in range(25)],
            items=[Panel.command("天气")],
            remark="天气功能",
        )

        result = await ConfigurationManager(api, panels=[panel]).sync()

        self.assertEqual(1, result.panels_created)
        create = next(call for call in api.calls if call[0] == "create_panel")
        self.assertEqual(20, len(create[4]))
        self.assertEqual("[botpy:weather] 天气功能", create[2]["remark"])
        target_call = next(call for call in api.calls if call[0] == "update_panel_targets")
        self.assertEqual(5, len(target_call[4]))

    async def test_existing_panel_is_updated_and_targets_are_reconciled(self):
        api = FakeApi()
        old = Panel(
            "admin",
            scope="group",
            target_type="specific",
            group_openids=["old", "keep"],
            items=[Panel.command("旧指令")],
        )
        await ConfigurationManager(api, panels=[old]).sync()
        api.calls.clear()
        desired = Panel(
            "admin",
            scope="group",
            target_type="specific",
            group_openids=["keep", "new"],
            items=[Panel.command("新指令", only_admin=True)],
        )

        result = await ConfigurationManager(api, panels=[desired]).sync()

        self.assertEqual(1, result.panels_updated)
        self.assertEqual(2, result.panel_targets_changed)
        self.assertEqual(["keep", "new"], api.details["panel-1"]["group_openids"])

    async def test_unmanaged_and_undeclared_panels_are_not_deleted(self):
        api = FakeApi()
        api.records.append(
            {
                "panel_id": "manual",
                "scope": "c2c",
                "target_type": "all",
                "panel": {"items": [], "remark": "manual"},
            }
        )

        await ConfigurationManager(api, panels=[Panel("managed", scope="c2c", items=[])]).sync()

        self.assertTrue(any(record["panel_id"] == "manual" for record in api.records))
        self.assertFalse(any(call[0] == "delete_panel" for call in api.calls))

    async def test_duplicate_remote_managed_key_fails_safely(self):
        api = FakeApi()
        for index, scope in enumerate(("c2c", "group"), 1):
            api.records.append(
                {
                    "panel_id": f"duplicate-{index}",
                    "scope": scope,
                    "target_type": "all",
                    "panel": {"items": [], "remark": "[botpy:same]"},
                }
            )

        with self.assertRaises(ConfigurationSyncError):
            await ConfigurationManager(api, panels=[Panel("same", scope="c2c", items=[])]).sync()
        self.assertFalse(any(call[0] in {"create_panel", "update_panel"} for call in api.calls))

    async def test_panel_capacity_is_checked_before_creating_anything(self):
        api = FakeApi()
        for index in range(20):
            api.records.append(
                {
                    "panel_id": f"manual-{index}",
                    "scope": "c2c",
                    "target_type": "all",
                    "panel": {"items": [], "remark": "manual"},
                }
            )

        with self.assertRaises(ConfigurationSyncError):
            await ConfigurationManager(api, panels=[Panel("managed", scope="c2c", items=[])]).sync()

        self.assertFalse(any(call[0] == "create_panel" for call in api.calls))

    async def test_malformed_remote_record_fails_without_creating(self):
        class MalformedApi(FakeApi):
            async def get_panels(self, scope, *, cursor=None, limit=20):
                return {"records": ["broken"] if scope == "c2c" else [], "next_cursor": "", "is_end": True}

        api = MalformedApi()
        with self.assertRaises(ConfigurationSyncError):
            await ConfigurationManager(api, panels=[Panel("managed", scope="c2c", items=[])]).sync()
        self.assertFalse(any(call[0] == "create_panel" for call in api.calls))

    async def test_terminal_empty_panel_page_may_omit_records_and_next_cursor(self):
        class MinimalEmptyPageApi(FakeApi):
            async def get_panels(self, scope, *, cursor=None, limit=20):
                self.calls.append(("get_panels", scope, cursor, limit))
                return {"is_end": True}

        api = MinimalEmptyPageApi()

        result = await ConfigurationManager(
            api,
            panels=[Panel("managed", scope="c2c", items=[])],
        ).sync()

        self.assertEqual(1, result.panels_created)
        self.assertEqual(4, sum(call[0] == "get_panels" for call in api.calls))

    async def test_non_terminal_panel_page_requires_non_empty_string_cursor(self):
        class InvalidCursorApi(FakeApi):
            def __init__(self, response):
                super().__init__()
                self.response = response

            async def get_panels(self, scope, *, cursor=None, limit=20):
                return self.response

        cases = (
            {"records": [], "is_end": False},
            {"records": [], "is_end": False, "next_cursor": ""},
            {"records": [], "is_end": False, "next_cursor": 123},
        )

        for response in cases:
            with self.subTest(response=response):
                manager = ConfigurationManager(
                    InvalidCursorApi(response),
                    panels=[Panel("managed", scope="c2c", items=[])],
                )
                with self.assertRaisesRegex(ConfigurationSyncError, "cursor"):
                    await manager.sync()

    async def test_terminal_panel_page_rejects_explicit_null_records(self):
        class NullRecordsApi(FakeApi):
            async def get_panels(self, scope, *, cursor=None, limit=20):
                return {"records": None, "is_end": True}

        with self.assertRaisesRegex(ConfigurationSyncError, "records"):
            await ConfigurationManager(
                NullRecordsApi(),
                panels=[Panel("managed", scope="c2c", items=[])],
            ).sync()

    async def test_non_terminal_panel_page_follows_cursor_to_minimal_final_page(self):
        class TwoPageApi(FakeApi):
            async def get_panels(self, scope, *, cursor=None, limit=20):
                self.calls.append(("get_panels", scope, cursor, limit))
                if cursor is None:
                    return {"is_end": False, "next_cursor": f"{scope}-next"}
                self.asserted_cursor = cursor
                return {"is_end": True}

        api = TwoPageApi()

        result = await ConfigurationManager(
            api,
            panels=[Panel("managed", scope="c2c", items=[])],
        ).sync()

        self.assertEqual(1, result.panels_created)
        panel_calls = [call for call in api.calls if call[0] == "get_panels"]
        self.assertEqual(8, len(panel_calls))
        for scope in ("c2c", "group", "channel", "dm"):
            self.assertIn(("get_panels", scope, f"{scope}-next", 50), panel_calls)

    async def test_non_terminal_panel_cursor_must_advance(self):
        class RepeatingCursorApi(FakeApi):
            async def get_panels(self, scope, *, cursor=None, limit=20):
                return {"records": [], "is_end": False, "next_cursor": "same"}

        manager = ConfigurationManager(
            RepeatingCursorApi(),
            panels=[Panel("managed", scope="c2c", items=[])],
        )

        with self.assertRaisesRegex(ConfigurationSyncError, "cursor"):
            await manager.sync()

    async def test_concurrent_sync_is_singleflight(self):
        class BlockingApi(FakeApi):
            def __init__(self):
                super().__init__()
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def get_menu(self):
                self.calls.append(("get_menu",))
                self.entered.set()
                await self.release.wait()
                return {"menu": self.menu, "version": self.menu_version}

        api = BlockingApi()
        manager = ConfigurationManager(api, menu=Menu.empty())
        first = asyncio.create_task(manager.sync())
        await api.entered.wait()
        second = asyncio.create_task(manager.sync())
        await asyncio.sleep(0)
        api.release.set()

        first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(first_result, second_result)
        self.assertEqual(1, sum(call[0] == "get_menu" for call in api.calls))

    async def test_cancelling_one_waiter_does_not_cancel_shared_operation(self):
        class BlockingApi(FakeApi):
            def __init__(self):
                super().__init__()
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def get_menu(self):
                self.entered.set()
                await self.release.wait()
                return {"menu": self.menu, "version": self.menu_version}

        api = BlockingApi()
        manager = ConfigurationManager(api, menu=Menu.empty())
        task = asyncio.create_task(manager.sync())
        await api.entered.wait()
        survivor = asyncio.create_task(manager.sync())
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        api.release.set()
        result = await survivor
        self.assertFalse(result.menu_changed)

    async def test_manager_close_cancels_shared_operation(self):
        class BlockingApi(FakeApi):
            def __init__(self):
                super().__init__()
                self.entered = asyncio.Event()
                self.cancelled = asyncio.Event()

            async def get_menu(self):
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise

        api = BlockingApi()
        manager = ConfigurationManager(api, menu=Menu.empty())
        waiter = asyncio.create_task(manager.sync())
        await api.entered.wait()
        await manager.close()

        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertTrue(api.cancelled.is_set())

    def test_declarations_validate_keys_targets_and_duplicates(self):
        with self.assertRaises(ValueError):
            Panel("bad key", scope="c2c", items=[])
        with self.assertRaises(ValueError):
            Panel("specific", scope="channel", target_type="specific", items=[])
        with self.assertRaises(ValueError):
            ConfigurationManager(
                FakeApi(),
                panels=[Panel("same", scope="c2c", items=[]), Panel("same", scope="group", items=[])],
            )
        with self.assertRaises(ValueError):
            Menu(items=[{"type": "send_message", "name": "帮助", "send_message": "/help", "link": "https://x"}])
        with self.assertRaises(ValueError):
            Panel("extra", scope="c2c", items=[{"type": "command", "name": "天气", "link": "https://x"}])

    async def test_client_syncs_after_login_before_gateway_discovery(self):
        calls = []

        class Http:
            async def login(self, token):
                calls.append("login")
                return {"id": "1", "username": "robot"}

        class Config:
            enabled = True

            async def sync(self):
                calls.append("sync")
                return type(
                    "Result",
                    (),
                    {
                        "menu_changed": False,
                        "panels_created": 0,
                        "panels_updated": 0,
                        "panel_targets_changed": 0,
                    },
                )()

        class Api:
            async def get_ws_url(self):
                calls.append("gateway")
                return {
                    "url": "wss://example.test",
                    "shards": 1,
                    "session_start_limit": {"max_concurrency": 1, "remaining": 1},
                }

        client = object.__new__(Client)
        client.http = Http()
        client.api = Api()
        client.configuration = Config()
        client._config_sync_strict = False
        client._transport_state = type("State", (), {"robot": None})()
        client._ws_ap = {}
        client.loop = asyncio.get_running_loop()
        await Client._bot_login(client, object(), use_gateway=True)

        self.assertEqual(["login", "sync", "gateway"], calls)

    async def test_client_soft_fails_configuration_sync_but_strict_reraises(self):
        class Http:
            def __init__(self):
                self.closed = False

            async def login(self, token):
                return {"id": "1", "username": "robot"}

            async def close(self):
                self.closed = True

        class Config:
            enabled = True

            async def sync(self):
                raise RuntimeError("sync failed")

        async def exercise(strict):
            client = object.__new__(Client)
            http = Http()
            client.http = http
            client.configuration = Config()
            client._config_sync_strict = strict
            client._transport_state = type("State", (), {"robot": None})()
            await Client._bot_login(client, object(), use_gateway=False)
            return http

        soft_http = await exercise(False)
        self.assertFalse(soft_http.closed)
        strict_client = object.__new__(Client)
        strict_http = Http()
        strict_client.http = strict_http
        strict_client.configuration = Config()
        strict_client._config_sync_strict = True
        strict_client._transport_state = type("State", (), {"robot": None})()
        with self.assertRaisesRegex(RuntimeError, "sync failed"):
            await Client._bot_login(strict_client, object(), use_gateway=False)
        self.assertTrue(strict_http.closed)
        self.assertIsNone(strict_client._robot)


if __name__ == "__main__":
    unittest.main()
