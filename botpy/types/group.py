# -*- coding: utf-8 -*-
from typing import List, TypedDict, Union


class GroupInfo(TypedDict):
    group_openid: str
    group_name: str
    group_finger_memo: str
    group_class_text: str
    group_tags: List[str]
    group_member_num: int


class GroupBotState(TypedDict):
    member_openid: str
    joined_at: str
    allow_proactive_msg: bool
    recv_msg_setting: str
    member_role: str


class ReviewQA(TypedDict):
    question: str
    answer: str


class VerifyInfo(TypedDict, total=False):
    method: str
    verify_message: str
    review_qa_list: List[ReviewQA]


class AutoApproved(TypedDict):
    strategy_id: str


class JoinRequest(TypedDict, total=False):
    join_request_id: str
    risk_tips: str
    union_openid: str
    member_openid: str
    username: str
    apply_at: str
    apply_source: str
    invited_by: str
    bot: bool
    verify_info: VerifyInfo
    auto_approved: AutoApproved


class JoinRequestList(TypedDict):
    list: List[JoinRequest]
    next_cursor: str


class MuteScheduleRule(TypedDict):
    task_id: str
    start_at: str
    end_at: str
    enabled: bool


class MuteRecurringRule(TypedDict):
    task_id: str
    weekdays: List[int]
    start_time: str
    end_time: str
    enabled: bool


class GlobalMuteRule(TypedDict):
    mode: str
    schedule_rules: List[MuteScheduleRule]
    recurring_rules: List[MuteRecurringRule]


class MemberMuteState(TypedDict):
    member_openid: str
    mute_expire_at: str
    username: str
    union_openid: str


class GroupMuteSetting(TypedDict):
    global_rule: GlobalMuteRule
    members: List[MemberMuteState]


class SetMemberMuteState(TypedDict, total=False):
    op: str
    member_openid: str
    mute_expire_at: str


GroupID = Union[int, str]


class GroupAction(TypedDict, total=False):
    op: str
    group_openids: List[str]
    group_ids: List[GroupID]


class JoinApprovalStrategy(TypedDict, total=False):
    strategy_id: str
    group_openids: List[str]
    group_ids: List[GroupID]
    whitelist_user_count: int
    is_enable: str
    expire_at: str
    created_at: str
    updated_at: str
    remark: str


class JoinApprovalStrategyList(TypedDict):
    strategies: List[JoinApprovalStrategy]
    next_cursor: str


class JoinApprovalStrategyResult(TypedDict):
    strategy_id: str
    is_enable: str
    expire_at: str


class JoinApprovalStrategyUpdateResult(TypedDict):
    is_enable: str
    expire_at: str


class JoinApprovalWhitelistResult(TypedDict):
    strategy_id: str
    whitelist_user_count: int
    updated_at: str
