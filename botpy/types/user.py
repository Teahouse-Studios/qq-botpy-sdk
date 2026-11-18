# -*- coding: utf-8 -*-
from typing import TypedDict, List


class User(TypedDict):
    id: str
    username: str
    avatar: str
    bot: bool
    union_openid: str
    union_user_account: str


class UserInfo(TypedDict, total=False):
    union_openid: str
    username: str
    avatar: str
    allow_proactive_msg: bool


class Member(TypedDict):
    user: User
    nick: str
    roles: List[str]
    joined_at: str


class GuildMemberPayload(Member):
    guild_id: str
