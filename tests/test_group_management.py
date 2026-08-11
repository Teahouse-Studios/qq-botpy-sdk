import asyncio
import unittest

from botpy.api import BotAPI
from botpy.client import Client
from botpy.connection import ConnectionState
from botpy.manage import GroupJoinRequestEvent, GroupMemberEvent


class RecordingHttp:
    def __init__(self):
        self.calls = []

    async def request(self, route, **kwargs):
        self.calls.append((route, kwargs))
        return {"ok": True}


class GroupManagementApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http = RecordingHttp()
        self.api = BotAPI(self.http)

    def assert_last_call(self, method, path, *, params=None, json=None):
        route, kwargs = self.http.calls[-1]
        self.assertEqual(method, route.method)
        self.assertEqual(path, route.path.format_map(route.parameters))
        if params is not None:
            self.assertEqual(params, kwargs.get("params"))
        if json is not None:
            self.assertEqual(json, kwargs.get("json"))

    async def test_group_info_and_bot_state_routes(self):
        await self.api.get_group_info("group")
        self.assert_last_call("GET", "/v2/groups/group/info")

        await self.api.get_group_bot_state("group")
        self.assert_last_call("GET", "/v2/groups/group/bot_state")

    async def test_group_join_request_list_uses_query_parameters(self):
        await self.api.get_group_join_requests("group", cursor="next", limit=50)

        self.assert_last_call(
            "GET",
            "/v2/groups/group/join_request_list",
            params={"cursor": "next", "limit": 50},
        )

    async def test_group_join_request_approval_helpers(self):
        await self.api.approve_group_join_request(
            "group",
            "member",
            join_request_id="request",
        )
        self.assert_last_call(
            "POST",
            "/v2/groups/group/approval_join_request/member",
            json={"op": "approve", "join_request_id": "request"},
        )

        await self.api.decline_group_join_request(
            "group",
            "member",
            join_request_id="request",
            reject_reason="blocked",
            add_to_member_blacklist=True,
        )
        self.assert_last_call(
            "POST",
            "/v2/groups/group/approval_join_request/member",
            json={
                "op": "decline",
                "join_request_id": "request",
                "reject_reason": "blocked",
                "add_to_member_blacklist": True,
            },
        )

    async def test_group_mute_query_and_update(self):
        await self.api.get_group_mute_setting("group")
        self.assert_last_call("GET", "/v2/groups/group/restrict_chat_setting")

        await self.api.set_group_member_mutes(
            "group",
            [
                {
                    "op": "add",
                    "member_openid": "member",
                    "mute_expire_at": "2026-08-12T00:00:00+08:00",
                }
            ],
        )
        self.assert_last_call(
            "POST",
            "/v2/groups/group/restrict_chat_setting",
            json={
                "members": [
                    {
                        "op": "add",
                        "member_openid": "member",
                        "mute_expire_at": "2026-08-12T00:00:00+08:00",
                    }
                ]
            },
        )

    async def test_join_approval_strategy_lifecycle(self):
        await self.api.get_group_join_approval_strategies(cursor="cursor", limit=10)
        self.assert_last_call(
            "GET",
            "/v2/groups/join_approval_strategy",
            params={"cursor": "cursor", "limit": 10},
        )

        await self.api.create_group_join_approval_strategy(
            group_openids=["group"],
            remark="strategy",
        )
        self.assert_last_call(
            "POST",
            "/v2/groups/join_approval_strategy",
            json={"group_openids": ["group"], "is_enable": "on", "remark": "strategy"},
        )

        await self.api.update_group_join_approval_strategy(
            "strategy",
            is_enable="off",
            group_action={"op": "add", "group_ids": [123456]},
        )
        self.assert_last_call(
            "PATCH",
            "/v2/groups/join_approval_strategy/strategy",
            json={
                "is_enable": "off",
                "group_action": {"op": "add", "group_ids": [123456]},
            },
        )

        await self.api.execute_group_join_approval_strategy("strategy")
        self.assert_last_call(
            "POST",
            "/v2/groups/join_approval_strategy/strategy/execute",
            json={},
        )

        await self.api.update_group_join_approval_whitelist(
            "strategy",
            op="add",
            whitelist_users=["1234567", "1234568"],
        )
        self.assert_last_call(
            "POST",
            "/v2/groups/join_approval_strategy/strategy/whitelist_users",
            json={"op": "add", "whitelist_users": ["1234567", "1234568"]},
        )

        await self.api.delete_group_join_approval_strategy("strategy")
        self.assert_last_call("DELETE", "/v2/groups/join_approval_strategy/strategy")

    async def test_group_api_validation_rejects_invalid_payloads(self):
        with self.assertRaises(ValueError):
            await self.api.get_group_join_requests("group", limit=101)
        with self.assertRaises(ValueError):
            await self.api.create_group_join_approval_strategy(
                group_openids=["group"],
                group_ids=[123456],
            )
        with self.assertRaises(ValueError):
            await self.api.update_group_join_approval_strategy("strategy")
        with self.assertRaises(ValueError):
            await self.api.update_group_join_approval_whitelist(
                "strategy",
                op="replace",
                whitelist_users=["1234567"],
            )


class GroupManagementEventTests(unittest.TestCase):
    def test_all_documented_group_management_events_use_canonical_callbacks(self):
        cases = {
            "group_add_robot": "group_add_robot",
            "group_del_robot": "group_del_robot",
            "group_msg_receive": "group_msg_receive",
            "group_msg_reject": "group_msg_reject",
            "group_member_add": "group_member_add",
            "group_member_remove": "group_member_remove",
            "group_join_request": "group_join_request",
        }

        for parser_name, callback_name in cases.items():
            with self.subTest(parser_name=parser_name):
                dispatched = []
                state = ConnectionState(lambda event, value: dispatched.append((event, value)), None)
                state.parsers[parser_name]({"id": "event-id", "d": {}})
                self.assertEqual(callback_name, dispatched[0][0])

    def test_group_member_event_exposes_user_openid_and_canonical_callback(self):
        dispatched = []
        state = ConnectionState(lambda event, value: dispatched.append((event, value)), None)

        state.parsers["group_member_add"](
            {
                "id": "event-id",
                "d": {
                    "timestamp": 1,
                    "group_openid": "group",
                    "member_openid": "member",
                    "user_openid": "user",
                },
            }
        )

        event_name, event = dispatched[0]
        self.assertEqual("group_member_add", event_name)
        self.assertIsInstance(event, GroupMemberEvent)
        self.assertEqual("event-id", event.event_id)
        self.assertEqual("user", event.user_openid)

    def test_group_join_request_event_is_parsed(self):
        dispatched = []
        state = ConnectionState(lambda event, value: dispatched.append((event, value)), None)

        state.parsers["group_join_request"](
            {
                "id": "event-id",
                "d": {
                    "group_openid": "group",
                    "join_request_id": "request",
                    "member_openid": "member",
                    "verify_info": {"method": "verify_message", "verify_message": "hello"},
                    "auto_approved": {"strategy_id": "strategy"},
                },
            }
        )

        event_name, event = dispatched[0]
        self.assertEqual("group_join_request", event_name)
        self.assertIsInstance(event, GroupJoinRequestEvent)
        self.assertEqual("request", event.join_request_id)
        self.assertEqual("verify_message", event.verify_info["method"])
        self.assertEqual("strategy", event.auto_approved["strategy_id"])

    def test_group_join_request_event_can_approve_or_decline_itself(self):
        class Api:
            def __init__(self):
                self.calls = []

            async def approve_group_join_request(self, *args, **kwargs):
                self.calls.append(("approve", args, kwargs))
                return {"ok": True}

            async def decline_group_join_request(self, *args, **kwargs):
                self.calls.append(("decline", args, kwargs))
                return {"ok": True}

        async def exercise():
            api = Api()
            event = GroupJoinRequestEvent(
                api,
                "event-id",
                {
                    "group_openid": "group",
                    "member_openid": "member",
                    "join_request_id": "request",
                },
            )
            await event.approve()
            await event.decline(reject_reason="blocked", add_to_member_blacklist=True)
            return api.calls

        calls = asyncio.run(exercise())
        self.assertEqual(
            (
                "approve",
                ("group", "member"),
                {"join_request_id": "request"},
            ),
            calls[0],
        )
        self.assertEqual("decline", calls[1][0])
        self.assertEqual("blocked", calls[1][2]["reject_reason"])
        self.assertTrue(calls[1][2]["add_to_member_blacklist"])

    def test_legacy_group_member_callback_is_used_as_fallback(self):
        scheduled = []

        class LegacyClient:
            async def on_message_group_member_add(self, event):
                return None

            def _schedule_event(self, callback, name, *args, **kwargs):
                scheduled.append(name)

        Client.ws_dispatch(LegacyClient(), "group_member_add", object())

        self.assertEqual(["on_message_group_member_add"], scheduled)


if __name__ == "__main__":
    unittest.main()
