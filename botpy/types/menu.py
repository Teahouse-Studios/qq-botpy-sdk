# -*- coding: utf-8 -*-
from typing import List, Literal, Optional, TypedDict


MenuItemType = Literal["switch", "send_message", "link", "menu"]
SubMenuItemType = Literal["send_message", "link"]


class Switch(TypedDict, total=False):
    switch_id: str
    default: bool


class SubMenuItem(TypedDict, total=False):
    name: str
    type: SubMenuItemType
    send_message: str
    link: str


class MenuItem(TypedDict, total=False):
    name: str
    type: MenuItemType
    sub_menu_items: List[SubMenuItem]
    send_message: str
    link: str
    switch: Switch


class Menu(TypedDict, total=False):
    items: List[MenuItem]


class GlobalMenuResponse(TypedDict, total=False):
    version: int
    menu: Optional[Menu]


class MenuVersionResponse(TypedDict):
    version: int
