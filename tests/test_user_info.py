import unittest

from botpy.api import BotAPI
from botpy.manage import GroupManageEvent, GroupMemberEvent
from botpy.message import C2CMessage, GroupMessage
from botpy.protocol import normalize_inbound_message, parse_gateway_event
from botpy.protocol.ratelimit import DEFAULT_ROUTE_RULES, RateLimitBudget, match_route


class RecordingHttp:
    def __init__(self):
        self.calls = []

    async def request(self, route, **kwargs):
        self.calls.append((route, kwargs))
        return {"ok": True}


class UserInfoApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http = RecordingHttp()
        self.api = BotAPI(self.http)

    async def test_get_user_info_route(self):
        result = await self.api.get_user_info("user-openid")

        self.assertEqual({"ok": True}, result)
        route, kwargs = self.http.calls[-1]
        self.assertEqual("GET", route.method)
        self.assertEqual("/v2/users/user-openid/info", route.path.format_map(route.parameters))
        self.assertEqual({}, kwargs)

    async def test_get_user_info_rejects_invalid_openid(self):
        for value in ("", None, 123):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    await self.api.get_user_info(value)

    def test_get_user_info_rate_limit(self):
        key = match_route("GET", "/v2/users/user-openid/info", DEFAULT_ROUTE_RULES)

        self.assertEqual("GET /v2/users/{}/info", key)
        self.assertEqual((RateLimitBudget(10, 1.0),), DEFAULT_ROUTE_RULES[key].budgets)


class UserInfoMessageTests(unittest.TestCase):
    def setUp(self):
        self.api = BotAPI(RecordingHttp())

    def test_group_message_author_and_union_group_openid(self):
        message = GroupMessage(
            self.api,
            "event-id",
            {
                "id": "message-id",
                "content": "hello",
                "group_openid": "group",
                "union_group_openid": "union-group",
                "author": {
                    "id": "author-id",
                    "username": "username",
                    "avatar": "https://example.invalid/avatar.png",
                    "bot": False,
                    "union_openid": "union-user",
                    "member_openid": "member",
                    "member_role": "admin",
                    "member_nickname": "nickname",
                },
            },
        )

        self.assertEqual("union-group", message.union_group_openid)
        self.assertEqual("author-id", message.author.id)
        self.assertEqual("username", message.author.username)
        self.assertEqual("https://example.invalid/avatar.png", message.author.avatar)
        self.assertFalse(message.author.bot)
        self.assertEqual("union-user", message.author.union_openid)
        self.assertEqual("member", message.author.member_openid)
        self.assertEqual("admin", message.author.member_role)
        self.assertEqual("nickname", message.author.member_nickname)

    def test_c2c_message_author_fields(self):
        message = C2CMessage(
            self.api,
            "event-id",
            {
                "id": "message-id",
                "content": "hello",
                "author": {
                    "id": "author-id",
                    "username": "username",
                    "avatar": "https://example.invalid/avatar.png",
                    "bot": False,
                    "union_openid": "union-user",
                    "user_openid": "user",
                },
            },
        )

        self.assertEqual("author-id", message.author.id)
        self.assertEqual("username", message.author.username)
        self.assertEqual("https://example.invalid/avatar.png", message.author.avatar)
        self.assertFalse(message.author.bot)
        self.assertEqual("union-user", message.author.union_openid)
        self.assertEqual("user", message.author.user_openid)


class UserInfoManageEventTests(unittest.TestCase):
    def setUp(self):
        self.api = BotAPI(RecordingHttp())

    def test_group_manage_event_exposes_union_group_openid(self):
        event = GroupManageEvent(
            self.api,
            "event-id",
            {
                "timestamp": 1,
                "group_openid": "group",
                "op_member_openid": "member",
                "union_group_openid": "union-group",
            },
        )

        self.assertEqual("union-group", event.union_group_openid)

    def test_group_member_event_exposes_union_group_openid(self):
        event = GroupMemberEvent(
            self.api,
            "event-id",
            {
                "timestamp": 1,
                "group_openid": "group",
                "member_openid": "member",
                "user_openid": "user",
                "union_group_openid": "union-group",
            },
        )

        self.assertEqual("union-group", event.union_group_openid)


class UserInfoNormalizationTests(unittest.TestCase):
    def test_group_message_author_prefers_member_nickname(self):
        data = {
            "id": "message-id",
            "content": "hello",
            "group_openid": "group",
            "union_group_openid": "union-group",
            "author": {
                "member_openid": "member",
                "username": "username",
                "member_nickname": "nickname",
                "avatar": "https://example.invalid/avatar.png",
                "union_openid": "union-user",
            },
        }

        message = normalize_inbound_message(parse_gateway_event({"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": data}))

        self.assertEqual("nickname", message.author_name)
        self.assertEqual("https://example.invalid/avatar.png", message.author_avatar)
        self.assertEqual("union-user", message.author_union_openid)
        self.assertEqual("union-group", message.metadata["union_group_openid"])

    def test_group_message_author_falls_back_to_username(self):
        data = {
            "id": "message-id",
            "content": "hello",
            "group_openid": "group",
            "author": {"member_openid": "member", "username": "username"},
        }

        message = normalize_inbound_message(parse_gateway_event({"op": 0, "t": "GROUP_MESSAGE_CREATE", "d": data}))

        self.assertEqual("username", message.author_name)

    def test_c2c_message_author_fields(self):
        data = {
            "id": "message-id",
            "content": "hello",
            "author": {
                "user_openid": "user",
                "username": "username",
                "avatar": "https://example.invalid/avatar.png",
                "union_openid": "union-user",
            },
        }

        message = normalize_inbound_message(parse_gateway_event({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": data}))

        self.assertEqual("username", message.author_name)
        self.assertEqual("https://example.invalid/avatar.png", message.author_avatar)
        self.assertEqual("union-user", message.author_union_openid)
