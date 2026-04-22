import os
import sys
import unittest
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import auto_sleep


class AutoSleepTriggerTest(unittest.TestCase):
    def test_trigger_sleep_falls_back_after_suspend_failures(self):
        with mock.patch.object(auto_sleep, "_enable_shutdown_privilege", return_value=True), \
             mock.patch.object(
                 auto_sleep,
                 "_call_set_suspend_state",
                 side_effect=[(False, 5), (False, 5)],
             ) as suspend_call, \
             mock.patch.object(
                 auto_sleep,
                 "_call_set_system_power_state",
                 side_effect=[(True, 0)],
             ) as power_call, \
             mock.patch.object(auto_sleep, "_run_rundll32_sleep", return_value=False) as shell_fallback:
            result = auto_sleep._trigger_sleep()

        self.assertTrue(result)
        self.assertEqual(suspend_call.call_args_list, [mock.call(False), mock.call(True)])
        self.assertEqual(power_call.call_args_list, [mock.call(False)])
        shell_fallback.assert_not_called()

    def test_trigger_sleep_uses_shell_fallback_when_all_powrprof_calls_fail(self):
        with mock.patch.object(auto_sleep, "_enable_shutdown_privilege", return_value=True), \
             mock.patch.object(
                 auto_sleep,
                 "_call_set_suspend_state",
                 side_effect=[(False, 87), (False, 87)],
             ), \
             mock.patch.object(
                 auto_sleep,
                 "_call_set_system_power_state",
                 side_effect=[(False, 50), (False, 50)],
             ), \
             mock.patch.object(auto_sleep, "_run_rundll32_sleep", return_value=True) as shell_fallback:
            result = auto_sleep._trigger_sleep()

        self.assertTrue(result)
        shell_fallback.assert_called_once_with()


class AutoSleepCountdownTest(unittest.TestCase):
    def setUp(self):
        auto_sleep._sleep_transition_event.clear()
        auto_sleep._sleep_transition_grace_until = 0.0
        auto_sleep._samples.clear()

    def test_do_countdown_resets_activity_after_sleep_failure(self):
        config = {"auto_sleep": {"countdown_seconds": 1}}
        auto_sleep._keyboard_activity_time = 0.0
        auto_sleep._mouse_activity_time = 0.0

        with mock.patch.object(auto_sleep, "_create_countdown_session", return_value=(mock.Mock(), mock.Mock())), \
             mock.patch.object(auto_sleep, "_wait_for_countdown_result", return_value=True), \
             mock.patch.object(auto_sleep, "_trigger_sleep", return_value=False), \
             mock.patch.object(auto_sleep.time, "time", return_value=1234.5):
            auto_sleep._do_countdown(config)

        self.assertEqual(auto_sleep._keyboard_activity_time, 1234.5)
        self.assertEqual(auto_sleep._mouse_activity_time, 1234.5)

    def test_do_countdown_resets_state_and_starts_post_sleep_grace_after_success(self):
        config = {"auto_sleep": {"countdown_seconds": 1, "idle_seconds": 10}}
        auto_sleep._keyboard_activity_time = 0.0
        auto_sleep._mouse_activity_time = 0.0
        auto_sleep._samples.extend([(1.0, 1.0, 1.0, 1.0)])

        with mock.patch.object(auto_sleep, "_create_countdown_session", return_value=(mock.Mock(), mock.Mock())), \
             mock.patch.object(auto_sleep, "_wait_for_countdown_result", return_value=True), \
             mock.patch.object(auto_sleep, "_trigger_sleep", return_value=True), \
             mock.patch.object(auto_sleep.time, "time", return_value=1234.5), \
             mock.patch.object(auto_sleep.time, "monotonic", return_value=200.0):
            auto_sleep._do_countdown(config)

        self.assertEqual(auto_sleep._keyboard_activity_time, 1234.5)
        self.assertEqual(auto_sleep._mouse_activity_time, 1234.5)
        self.assertEqual(len(auto_sleep._samples), 0)
        self.assertFalse(auto_sleep._sleep_transition_event.is_set())
        self.assertEqual(auto_sleep._sleep_transition_grace_until, 215.0)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()