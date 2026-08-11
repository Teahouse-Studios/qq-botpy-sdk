from typing import Dict
from .api import BotAPI


class GroupManageEvent:
    __slots__ = (
        "_api",
        "event_id",
        "timestamp",
        "group_openid",
        "op_member_openid",
    )

    def __init__(self, api: BotAPI, event_id, data: Dict):
        self._api = api
        self.event_id = event_id
        self.timestamp = data.get("timestamp", None)
        self.group_openid = data.get("group_openid", None)
        self.op_member_openid = data.get("op_member_openid", None)

    def __repr__(self):
        return str({items: str(getattr(self, items)) for items in self.__slots__ if not items.startswith("_")})


class C2CManageEvent:
    __slots__ = (
        "_api",
        "event_id",
        "timestamp",
        "openid",
    )

    def __init__(self, api: BotAPI, event_id, data: Dict):
        self._api = api
        self.event_id = event_id
        self.timestamp = data.get("timestamp", None)
        self.openid = data.get("openid", None)

    def __repr__(self):
        return str({items: str(getattr(self, items)) for items in self.__slots__ if not items.startswith("_")})


class GroupMemberEvent:
    __slots__ = (
        "_api",
        "event_id",
        "timestamp",
        "group_openid",
        "member_openid",
        "user_openid",
    )

    def __init__(self, api: BotAPI, event_id, data: Dict):
        self._api = api
        self.event_id = event_id
        self.timestamp = data.get("timestamp", None)
        self.group_openid = data.get("group_openid", None)
        self.member_openid = data.get("member_openid", None)
        self.user_openid = data.get("user_openid", None)

    def __repr__(self):
        return str({items: str(getattr(self, items)) for items in self.__slots__ if not items.startswith("_")})


class GroupJoinRequestEvent:
    __slots__ = (
        "_api",
        "event_id",
        "group_openid",
        "join_request_id",
        "risk_tips",
        "union_openid",
        "member_openid",
        "username",
        "apply_at",
        "apply_source",
        "invited_by",
        "bot",
        "verify_info",
        "auto_approved",
    )

    def __init__(self, api: BotAPI, event_id, data: Dict):
        self._api = api
        self.event_id = event_id
        self.group_openid = data.get("group_openid", None)
        self.join_request_id = data.get("join_request_id", None)
        self.risk_tips = data.get("risk_tips", None)
        self.union_openid = data.get("union_openid", None)
        self.member_openid = data.get("member_openid", None)
        self.username = data.get("username", None)
        self.apply_at = data.get("apply_at", None)
        self.apply_source = data.get("apply_source", None)
        self.invited_by = data.get("invited_by", None)
        self.bot = data.get("bot", None)
        self.verify_info = data.get("verify_info", None)
        self.auto_approved = data.get("auto_approved", None)

    async def approve(self):
        """通过当前入群申请。"""

        if not self.group_openid or not self.member_openid:
            raise ValueError("group_openid and member_openid are required to approve a join request")
        return await self._api.approve_group_join_request(
            self.group_openid,
            self.member_openid,
            join_request_id=self.join_request_id,
        )

    async def decline(self, *, reject_reason=None, add_to_member_blacklist=False):
        """拒绝当前入群申请，并可同时加入群黑名单。"""

        if not self.group_openid or not self.member_openid:
            raise ValueError("group_openid and member_openid are required to decline a join request")
        return await self._api.decline_group_join_request(
            self.group_openid,
            self.member_openid,
            join_request_id=self.join_request_id,
            reject_reason=reject_reason,
            add_to_member_blacklist=add_to_member_blacklist,
        )

    def __repr__(self):
        return str({items: str(getattr(self, items)) for items in self.__slots__ if not items.startswith("_")})
