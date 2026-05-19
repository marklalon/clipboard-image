import asyncio
import json
import os
import threading
import time
import urllib.request
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import monitor_server


try:
    import websockets
except Exception:
    websockets = None


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

    @unittest.skipUnless(
        monitor_server.monitor_server_dependencies_available()[0] and websockets is not None,
        "Monitor server dependencies are unavailable",
    )
    def test_healthcheck_stays_responsive_while_websocket_snapshot_blocks(self):
        controller = monitor_server.MonitorServerController()
        original_get_snapshot = monitor_server.system_overlay.get_monitor_snapshot
        snapshot_started = threading.Event()
        websocket_done = threading.Event()
        errors = []
        blocking_seconds = 0.35

        def _pick_free_port() -> int:
            import socket

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                return sock.getsockname()[1]

        def fake_get_monitor_snapshot(*args, **kwargs):
            snapshot_started.set()
            time.sleep(blocking_seconds)
            return {
                "timestamp": "2026-01-01T00:00:00Z",
                "sources": {"nvml": False, "lhm": False},
                "stats": {"cpu_pct": 10},
            }

        async def receive_ws_message(ws_url: str) -> None:
            async with websockets.connect(ws_url) as websocket:
                await asyncio.wait_for(websocket.recv(), timeout=2)

        def websocket_worker(ws_url: str) -> None:
            try:
                asyncio.run(receive_ws_message(ws_url))
            except Exception as exc:
                errors.append(exc)
            finally:
                websocket_done.set()

        port = _pick_free_port()
        monitor_server.system_overlay.get_monitor_snapshot = fake_get_monitor_snapshot

        try:
            controller.start(
                {
                    "monitor_server": {
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": port,
                        "mdns": False,
                    }
                }
            )

            ws_thread = threading.Thread(
                target=websocket_worker,
                args=(f"ws://127.0.0.1:{port}/ws/monitor",),
                daemon=True,
            )
            ws_thread.start()

            self.assertTrue(snapshot_started.wait(timeout=1), "websocket snapshot did not start")

            start = time.perf_counter()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                body = response.read().decode("utf-8")
            elapsed = time.perf_counter() - start

            self.assertIn('"status":"ok"', body.replace(" ", ""))
            self.assertLess(
                elapsed,
                blocking_seconds * 0.5,
                "healthcheck should not wait for websocket snapshot generation",
            )

            ws_thread.join(timeout=2)
            self.assertFalse(ws_thread.is_alive(), "websocket worker did not finish")
            self.assertFalse(errors, f"websocket worker error: {errors}")
            self.assertTrue(websocket_done.is_set())
        finally:
            monitor_server.system_overlay.get_monitor_snapshot = original_get_snapshot
            controller.stop()

    @unittest.skipUnless(
        monitor_server.monitor_server_dependencies_available()[0] and websockets is not None,
        "Monitor server dependencies are unavailable",
    )
    def test_websocket_waits_for_fresh_snapshot_when_cache_is_stale(self):
        controller = monitor_server.MonitorServerController()
        overlay = monitor_server.system_overlay
        original_get_system_stats = overlay.get_system_stats
        original_get_gpu_stats = overlay.get_gpu_stats
        original_snapshot_cache = overlay._snapshot_cache
        original_snapshot_cache_at = overlay._snapshot_cache_at
        original_snapshot_building = dict(overlay._snapshot_building)
        refresh_started = threading.Event()
        refresh_finished = threading.Event()
        blocking_seconds = 0.35

        def _pick_free_port() -> int:
            import socket

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                return sock.getsockname()[1]

        def _system_stats(cpu_pct: int) -> dict:
            return {
                "ram_used_gb": 1.0,
                "ram_total_gb": 2.0,
                "ram_pct": 50.0,
                "ram_temps": None,
                "ram_temp_c": None,
                "cpu_pct": float(cpu_pct),
                "cpu_temp_c": None,
                "cpu_power_w": None,
            }

        def fast_system_stats() -> dict:
            return _system_stats(10)

        def slow_system_stats() -> dict:
            refresh_started.set()
            time.sleep(blocking_seconds)
            refresh_finished.set()
            return _system_stats(20)

        def fake_gpu_stats() -> dict:
            return {
                "vram_used_mb": None,
                "vram_total_mb": None,
                "gpu_util_pct": None,
                "gpu_temp_c": None,
                "gpu_power_w": None,
            }

        async def receive_ws_message(ws_url: str) -> dict:
            async with websockets.connect(ws_url) as websocket:
                raw_message = await asyncio.wait_for(websocket.recv(), timeout=2)
            return json.loads(raw_message)

        port = _pick_free_port()
        overlay.get_system_stats = fast_system_stats
        overlay.get_gpu_stats = fake_gpu_stats
        overlay._snapshot_cache = None
        overlay._snapshot_cache_at = 0.0
        overlay._snapshot_building = {
            "default": False,
            "disk": False,
            "fan": False,
        }

        try:
            controller.start(
                {
                    "monitor_server": {
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": port,
                        "mdns": False,
                    }
                }
            )

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/monitor", timeout=1) as response:
                primed_snapshot = json.loads(response.read().decode("utf-8"))

            self.assertEqual(primed_snapshot["stats"]["cpu_pct"], 10.0)

            time.sleep(0.55)
            overlay.get_system_stats = slow_system_stats

            ws_url = f"ws://127.0.0.1:{port}/ws/monitor?interval_ms=200"
            start = time.perf_counter()
            payload = asyncio.run(receive_ws_message(ws_url))
            elapsed = time.perf_counter() - start

            self.assertGreaterEqual(
                elapsed,
                blocking_seconds * 0.8,
                "websocket should wait for a fresh snapshot when cache is stale",
            )
            self.assertTrue(refresh_started.wait(timeout=1), "synchronous refresh did not start")
            self.assertEqual(payload["type"], "snapshot")
            self.assertEqual(payload["payload"]["stats"]["cpu_pct"], 20.0)

            self.assertTrue(refresh_finished.wait(timeout=0.1), "synchronous refresh did not finish")

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/monitor", timeout=1) as response:
                refreshed_snapshot = json.loads(response.read().decode("utf-8"))

            self.assertEqual(refreshed_snapshot["stats"]["cpu_pct"], 20.0)
        finally:
            overlay.get_system_stats = original_get_system_stats
            overlay.get_gpu_stats = original_get_gpu_stats
            overlay._snapshot_cache = original_snapshot_cache
            overlay._snapshot_cache_at = original_snapshot_cache_at
            overlay._snapshot_building = original_snapshot_building
            controller.stop()


if __name__ == "__main__":
    unittest.main()