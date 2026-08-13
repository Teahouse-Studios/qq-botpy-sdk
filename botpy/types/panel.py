# -*- coding: utf-8 -*-
from typing import List, Literal, TypedDict


PanelScope = Literal["c2c", "group", "channel", "dm"]
PanelTargetType = Literal["all", "specific"]
PanelItemType = Literal["command", "link"]
PanelTargetOperation = Literal["add", "del"]


class PanelItem(TypedDict, total=False):
    name: str
    desc: str
    type: PanelItemType
    only_admin: bool
    link: str


class Panel(TypedDict, total=False):
    items: List[PanelItem]
    remark: str
    version: int


class PanelRecord(TypedDict, total=False):
    panel_id: str
    scope: PanelScope
    target_type: PanelTargetType
    panel: Panel
    created_at: str
    updated_at: str
    version: int


class PanelListResponse(TypedDict):
    records: List[PanelRecord]
    next_cursor: str
    is_end: bool


class PanelDetail(PanelRecord, total=False):
    user_openids: List[str]
    group_openids: List[str]


class PanelCreateResponse(TypedDict):
    panel_id: str


class PanelVersionResponse(TypedDict):
    version: int
