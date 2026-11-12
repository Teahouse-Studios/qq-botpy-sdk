import copy
import unittest

from botpy.api import BotAPI
from botpy.types.inline import normalize_keyboard


class RecordingHttp:
    def __init__(self):
        self.calls = []

    async def request(self, route, **kwargs):
        self.calls.append((route, kwargs))
        return {"ok": True}


class InlineKeyboardTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_small_button_option_without_mutating_input(self):
        keyboard = {
            "small": True,
            "content": {"rows": []},
        }

        normalized = normalize_keyboard(keyboard)

        self.assertEqual(
            {"content": {"rows": [], "style": {"font_size": "small"}}},
            normalized,
        )
        self.assertEqual({"small": True, "content": {"rows": []}}, keyboard)

    def test_nested_button_option_is_normalized(self):
        keyboard = {
            "content": {
                "rows": [
                    {
                        "buttons": [
                            {"id": "one", "small": True},
                            {"id": "two"},
                        ]
                    }
                ]
            }
        }

        self.assertEqual(
            {
                "content": {
                    "rows": [{"buttons": [{"id": "one"}, {"id": "two"}]}],
                    "style": {"font_size": "small"},
                }
            },
            normalize_keyboard(keyboard),
        )

    def test_small_false_removes_only_small_font_size(self):
        keyboard = {
            "small": False,
            "content": {
                "rows": [],
                "style": {"font_size": "small", "color": "blue"},
            },
        }

        self.assertEqual(
            {"content": {"rows": [], "style": {"color": "blue"}}},
            normalize_keyboard(keyboard),
        )

    async def test_bot_api_normalizes_keyboard_before_request(self):
        http = RecordingHttp()
        api = BotAPI(http)
        keyboard = {"small": True, "content": {"rows": []}}
        original = copy.deepcopy(keyboard)

        await api.post_message("channel", keyboard=keyboard)

        self.assertEqual(
            {"keyboard": {"content": {"rows": [], "style": {"font_size": "small"}}}},
            http.calls[-1][1]["json"],
        )
        self.assertEqual(original, keyboard)

    async def test_post_keyboard_message_uses_official_keyboard_shape(self):
        http = RecordingHttp()
        api = BotAPI(http)
        keyboard = {"small": True, "content": {"rows": []}}

        await api.post_keyboard_message("channel", keyboard=keyboard)

        self.assertEqual(
            {"small": True, "content": {"rows": []}},
            keyboard,
        )
        self.assertEqual(
            {
                "keyboard": {
                    "content": {
                        "rows": [],
                        "style": {"font_size": "small"},
                    }
                },
                "markdown": None,
            },
            http.calls[-1][1]["json"],
        )
