import unittest

from botpy.protocol.reconnect import RATE_LIMIT_DELAY, ReconnectPolicy


class ReconnectPolicyTests(unittest.TestCase):
    def test_retry_delays_increase_and_cap(self):
        policy = ReconnectPolicy(delays=(1, 2, 5), max_attempts=5)

        self.assertEqual([1, 2, 5, 5, 5, None], [policy.next_delay() for _ in range(6)])

    def test_connected_resets_retry_attempts(self):
        policy = ReconnectPolicy(delays=(1, 2))
        policy.next_delay()
        policy.next_delay()

        policy.on_connected()

        self.assertEqual(1, policy.next_delay())

    def test_gateway_close_codes_are_classified(self):
        policy = ReconnectPolicy()

        auth = policy.handle_close(4004)
        invalid_session = policy.handle_close(4007)
        rate_limited = policy.handle_close(4008)
        fatal = policy.handle_close(4915)

        self.assertTrue(auth.refresh_token)
        self.assertFalse(auth.clear_session)
        self.assertTrue(invalid_session.clear_session)
        self.assertTrue(invalid_session.refresh_token)
        self.assertEqual(RATE_LIMIT_DELAY, rate_limited.reconnect_delay)
        self.assertTrue(fatal.fatal)
        self.assertFalse(fatal.should_reconnect)

    def test_three_quick_disconnects_trigger_cooldown(self):
        now = [0.0]
        policy = ReconnectPolicy(clock=lambda: now[0])

        actions = []
        for _ in range(3):
            policy.on_connected()
            now[0] += 1
            actions.append(policy.handle_close(1006))

        self.assertIsNone(actions[0].reconnect_delay)
        self.assertIsNone(actions[1].reconnect_delay)
        self.assertEqual(RATE_LIMIT_DELAY, actions[2].reconnect_delay)

    def test_normal_and_client_closure_do_not_reconnect(self):
        policy = ReconnectPolicy()

        self.assertFalse(policy.handle_close(1000).should_reconnect)
        self.assertFalse(policy.handle_close(1006, closing=True).should_reconnect)


if __name__ == "__main__":
    unittest.main()
