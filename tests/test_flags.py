import unittest

import botpy


class IntentsTestCase(unittest.TestCase):
    def test_none(self):
        intents = botpy.Intents.none()
        self.assertEqual(intents.value, 0)  # add assertion here

    def test_multi_intents(self):
        intents = botpy.Intents(guilds=True, guild_messages=True)
        self.assertEqual(513, intents.value)

    def test_group_member_event(self):
        intents = botpy.Intents(group_member_event=True)
        self.assertEqual(1 << 24, intents.value)
        self.assertTrue(intents.group_member_event)

        intents.group_member_event = False
        self.assertEqual(0, intents.value)

    def test_group_member_event_combines_with_public_messages(self):
        intents = botpy.Intents(group_member_event=True, public_messages=True)
        self.assertEqual((1 << 24) | (1 << 25), intents.value)

    def test_all_includes_group_member_event(self):
        intents = botpy.Intents.all()
        self.assertTrue(intents.group_member_event)

    def test_default(self):
        intents = botpy.Intents.default()
        self.assertEqual(1863062531, intents.value)
        self.assertTrue(intents.group_member_event)


if __name__ == "__main__":
    unittest.main()
