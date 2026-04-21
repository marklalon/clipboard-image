import os
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import monitor_server


class MonitorServerHelpersTest(unittest.TestCase):
    def test_normalize_monitor_server_config_applies_defaults_and_limits(self):
        cfg = monitor_server.normalize_monitor_server_config(
            {
                "monitor_server": {
                    "enabled": True,
                    "host": "  ",
                    "port": 99999,
                    "token": "  secret  ",
                }
            }
        )

        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["host"], monitor_server.DEFAULT_HOST)
        self.assertEqual(cfg["port"], 65535)
        self.assertEqual(cfg["token"], "secret")

    def test_extract_request_token_prefers_bearer_header(self):
        token = monitor_server._extract_request_token(
            {
                "authorization": "Bearer abc123",
                "x-monitor-token": "other",
            },
            {"token": "query-token"},
        )

        self.assertEqual(token, "abc123")

    def test_is_authorized_accepts_query_token(self):
        self.assertTrue(
            monitor_server._is_authorized(
                "hello",
                {},
                {"token": "hello"},
            )
        )
        self.assertFalse(
            monitor_server._is_authorized(
                "hello",
                {},
                {"token": "nope"},
            )
        )

    def test_parse_interval_ms_clamps_invalid_values(self):
        self.assertEqual(monitor_server._parse_interval_ms("bad"), monitor_server.DEFAULT_WS_INTERVAL_MS)
        self.assertEqual(monitor_server._parse_interval_ms(1), monitor_server.MIN_WS_INTERVAL_MS)
        self.assertEqual(monitor_server._parse_interval_ms(999999), monitor_server.MAX_WS_INTERVAL_MS)


if __name__ == "__main__":
    unittest.main()