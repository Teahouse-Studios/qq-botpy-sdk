from collections.abc import Mapping
from copy import deepcopy
from typing import Any, List, Literal, Optional, TypedDict


class Permission(TypedDict):
    type: int
    specify_role_ids: List[str]
    specify_user_ids: List[str]


class RenderData(TypedDict):
    label: str
    visited_label: str
    style: int


class Action(TypedDict):
    type: int
    permission: Permission
    click_limit: int
    data: str
    at_bot_show_channel_list: bool


class _SmallButtonOptions(TypedDict, total=False):
    small: bool


class Button(_SmallButtonOptions):
    id: str
    render_data: RenderData
    action: Action


class KeyboardRow(TypedDict):
    buttons: List[Button]


class KeyboardStyle(TypedDict, total=False):
    font_size: Literal["small"]


class _KeyboardContentOptions(TypedDict, total=False):
    style: KeyboardStyle


class KeyboardContent(_KeyboardContentOptions):
    rows: List[KeyboardRow]


class Keyboard(_SmallButtonOptions):
    content: KeyboardContent


_SMALL_BUTTON_NESTED_KEYS = ("data", "buttons", "button", "content", "rows")


def _find_small_setting(value: Any) -> Optional[bool]:
    """Find a small-button setting in a keyboard or one of its button entries."""

    if isinstance(value, Mapping):
        setting = value.get("small")
        if isinstance(setting, bool):
            return setting

        style = value.get("style")
        if isinstance(style, Mapping) and style.get("font_size") == "small":
            return True

        nested_settings = [
            _find_small_setting(value[key])
            for key in _SMALL_BUTTON_NESTED_KEYS
            if key in value
        ]
    elif isinstance(value, (list, tuple)):
        nested_settings = [_find_small_setting(item) for item in value]
    else:
        return None

    if any(setting is True for setting in nested_settings):
        return True
    if any(setting is False for setting in nested_settings):
        return False
    return None


def _strip_small_button_flags(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _strip_small_button_flags(item)
            for key, item in value.items()
            if key != "small"
        }
    if isinstance(value, list):
        return [_strip_small_button_flags(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_small_button_flags(item) for item in value)
    return value


def _set_keyboard_font_size(value: Any, enabled: bool) -> Any:
    if not isinstance(value, Mapping):
        return value

    result = dict(value)
    if "rows" in result:
        style = result.get("style")
        style_result = dict(style) if isinstance(style, Mapping) else {}
        if enabled:
            style_result["font_size"] = "small"
            result["style"] = style_result
        elif style_result.get("font_size") == "small":
            style_result.pop("font_size", None)
            if style_result:
                result["style"] = style_result
            else:
                result.pop("style", None)
        return result

    content = result.get("content")
    if isinstance(content, Mapping):
        result["content"] = _set_keyboard_font_size(content, enabled)
    return result


def normalize_keyboard(keyboard: Mapping[str, Any]) -> dict:
    """Convert the SDK's ``small`` convenience option to QQ's wire format.

    The returned mapping is a deep copy, so normalizing a payload never changes
    a keyboard object that the caller may reuse.
    """

    if not isinstance(keyboard, Mapping):
        return keyboard

    copied = deepcopy(dict(keyboard))
    small_setting = _find_small_setting(copied)
    normalized = _strip_small_button_flags(copied)
    if small_setting is not None:
        normalized = _set_keyboard_font_size(normalized, small_setting)
    return normalized
