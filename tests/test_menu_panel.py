import unittest

from botpy.api import BotAPI


MENU = {
    "items": [
        {
            "type": "send_message",
            "name": "帮助",
            "send_message": "/help",
        },
        {
            "type": "link",
            "name": "官网",
            "link": "https://example.com",
        },
        {
            "type": "menu",
            "name": "更多",
            "sub_menu_items": [
                {
                    "type": "send_message",
                    "name": "设置",
                    "send_message": "/settings",
                }
            ],
        },
        {
            "type": "switch",
            "name": "搜索",
            "switch": {"switch_id": "search", "default": False},
        },
    ]
}

PANEL = {
    "items": [
        {
            "type": "command",
            "name": "查询天气",
            "desc": "查询当前天气",
            "only_admin": False,
        },
        {
            "type": "link",
            "name": "更多服务",
            "desc": "打开服务页面",
            "link": "https://example.com/services",
        },
    ],
    "remark": "测试面板",
}


class RecordingHttp:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or ())

    async def request(self, route, **kwargs):
        self.calls.append((route, kwargs))
        if self.responses:
            return self.responses.pop(0)
        return {"ok": True}


class MenuPanelApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.http = RecordingHttp()
        self.api = BotAPI(self.http)

    def assert_last_call(self, method, path, *, params=None, json=None):
        route, kwargs = self.http.calls[-1]
        self.assertEqual(method, route.method)
        self.assertEqual(path, route.path.format_map(route.parameters))
        if params is None:
            self.assertNotIn("params", kwargs)
        else:
            self.assertEqual(params, kwargs.get("params"))
        if json is None:
            self.assertNotIn("json", kwargs)
        else:
            self.assertEqual(json, kwargs.get("json"))

    async def test_get_menu_uses_global_menu_route_and_returns_response(self):
        expected = {"menu": MENU, "version": 3}
        self.http.responses.append(expected)

        result = await self.api.get_menu()

        self.assertIs(expected, result)
        self.assert_last_call("GET", "/v2/menu")

    async def test_update_menu_wraps_menu_configuration(self):
        expected = {"version": 4}
        self.http.responses.append(expected)

        result = await self.api.update_menu(MENU)

        self.assertIs(expected, result)
        self.assert_last_call("PUT", "/v2/menu", json={"menu": MENU})

    async def test_get_panels_uses_scope_cursor_and_limit_query_parameters(self):
        expected = {"records": [], "next_cursor": "", "is_end": True}
        self.http.responses.append(expected)

        result = await self.api.get_panels("c2c", cursor="next-page", limit=10)

        self.assertIs(expected, result)
        self.assert_last_call(
            "GET",
            "/v2/panels",
            params={"scope": "c2c", "cursor": "next-page", "limit": 10},
        )

    async def test_get_panels_omits_absent_cursor(self):
        await self.api.get_panels("channel")

        self.assert_last_call(
            "GET",
            "/v2/panels",
            params={"scope": "channel", "limit": 20},
        )

    async def test_get_panels_returns_minimal_terminal_empty_page_unchanged(self):
        expected = {"is_end": True}
        self.http.responses.append(expected)

        result = await self.api.get_panels("dm")

        self.assertIs(expected, result)
        self.assert_last_call(
            "GET",
            "/v2/panels",
            params={"scope": "dm", "limit": 20},
        )

    async def test_create_global_panel_sends_scope_target_and_panel(self):
        expected = {"panel_id": "panel-global"}
        self.http.responses.append(expected)

        result = await self.api.create_panel(
            "c2c",
            PANEL,
            target_type="all",
        )

        self.assertIs(expected, result)
        self.assert_last_call(
            "POST",
            "/v2/panels",
            json={"scope": "c2c", "target_type": "all", "panel": PANEL},
        )

    async def test_create_specific_panels_include_only_the_matching_targets(self):
        await self.api.create_panel(
            "c2c",
            PANEL,
            target_type="specific",
            user_openids=["user-1", "user-2"],
        )
        self.assert_last_call(
            "POST",
            "/v2/panels",
            json={
                "scope": "c2c",
                "target_type": "specific",
                "user_openids": ["user-1", "user-2"],
                "panel": PANEL,
            },
        )

        await self.api.create_panel(
            "group",
            PANEL,
            target_type="specific",
            group_openids=["group-1", "group-2"],
        )
        self.assert_last_call(
            "POST",
            "/v2/panels",
            json={
                "scope": "group",
                "target_type": "specific",
                "group_openids": ["group-1", "group-2"],
                "panel": PANEL,
            },
        )

    async def test_get_update_and_delete_panel_use_panel_id_route(self):
        detail = {"panel_id": "panel-1", "scope": "group", "panel": PANEL}
        updated = {"version": 2}
        self.http.responses.extend((detail, updated, None))

        self.assertIs(detail, await self.api.get_panel("panel-1"))
        self.assert_last_call("GET", "/v2/panels/panel-1")

        self.assertIs(updated, await self.api.update_panel("panel-1", PANEL))
        self.assert_last_call(
            "PUT",
            "/v2/panels/panel-1",
            json={"panel": PANEL},
        )

        self.assertIsNone(await self.api.delete_panel("panel-1"))
        self.assert_last_call("DELETE", "/v2/panels/panel-1")

    async def test_update_panel_targets_supports_user_and_group_add_delete(self):
        cases = (
            ("add", {"user_openids": ["user-1"]}),
            ("del", {"user_openids": ["user-2"]}),
            ("add", {"group_openids": ["group-1"]}),
            ("del", {"group_openids": ["group-2"]}),
        )

        for op, targets in cases:
            with self.subTest(op=op, targets=targets):
                result = await self.api.update_panel_targets("panel-1", op=op, **targets)

                self.assertEqual({"ok": True}, result)
                self.assert_last_call(
                    "PUT",
                    "/v2/panels/panel-1/target",
                    json={"op": op, **targets},
                )

    async def test_invalid_https_urls_fail_before_request(self):
        for url in ("https://", "https:///path", "http://example.com", "https://example.com\n/path"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await self.api.update_menu(
                    {"items": [{"type": "link", "name": "官网", "link": url}]}
                )
        self.assertEqual([], self.http.calls)


if __name__ == "__main__":
    unittest.main()
