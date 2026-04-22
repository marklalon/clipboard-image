import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import system_overlay


class _FakeDateTime:
    _counter = 0

    @classmethod
    def now(cls, _tz):
        cls._counter += 1
        return datetime(2026, 1, 1, 0, 0, cls._counter, tzinfo=timezone.utc)


class _FakeEnumValue:
    def __init__(self, value):
        self._value = value

    def ToString(self):
        return self._value


class _FakeSensor:
    def __init__(self, name, sensor_type="Temperature", value=42.0):
        self.Name = name
        self.SensorType = _FakeEnumValue(sensor_type)
        self.Value = value


class _FakeStorageProperty:
    def __init__(self, storage):
        self._storage = storage

    def GetValue(self, _hardware):
        return self._storage


class _FakeHardwareType:
    def __init__(self, storage):
        self._storage = storage

    def GetProperty(self, name):
        if name == "Storage":
            return _FakeStorageProperty(self._storage)
        return None


class _FakeStorage:
    def __init__(self, serial_number, drive_number=None, model=None):
        self.SerialNumber = serial_number
        self.DriveNumber = drive_number
        self.Model = model
        self.Smart = None


class _FakeHardware:
    def __init__(self, name, sensors=None, subhardware=None, hardware_type="Storage", storage=None):
        self.Name = name
        self.Sensors = sensors or []
        self.SubHardware = subhardware or []
        self.HardwareType = _FakeEnumValue(hardware_type)
        self._storage = storage

    def Update(self):
        return None

    def GetType(self):
        return _FakeHardwareType(self._storage)


class _FakeComputer:
    def __init__(self, hardware):
        self.Hardware = hardware


class SystemOverlayHelpersTest(unittest.TestCase):
    def setUp(self):
        self._old_cache = system_overlay._snapshot_cache
        self._old_cache_at = system_overlay._snapshot_cache_at
        system_overlay._snapshot_cache = None
        system_overlay._snapshot_cache_at = 0.0
        _FakeDateTime._counter = 0

    def tearDown(self):
        system_overlay._snapshot_cache = self._old_cache
        system_overlay._snapshot_cache_at = self._old_cache_at
        system_overlay._lhm_computer = None
        system_overlay._lhm_disk_temps = {}
        system_overlay._lhm_disk_storage = {}
        system_overlay._lhm_disk_display_name_lookup = {}
        system_overlay._overlay_instance = None

    def test_assign_unique_disk_names_appends_serial_suffix_for_duplicates(self):
        names = [
            "ZHITAI Ti600 4TB",
            "Samsung SSD 980 PRO 2TB",
            "ZHITAI Ti600 4TB",
        ]
        suffix_map = {
            "ZHITAI Ti600 4TB": ["00C5", "0005"],
        }

        result = system_overlay._assign_unique_disk_names(names, suffix_map)

        self.assertEqual(
            result,
            [
                "ZHITAI Ti600 4TB (00C5)",
                "Samsung SSD 980 PRO 2TB",
                "ZHITAI Ti600 4TB (0005)",
            ],
        )

    def test_assign_unique_disk_names_suffixes_single_sensor_when_model_is_globally_duplicated(self):
        names = [
            "ZHITAI Ti600 4TB",
            "Samsung SSD 980 PRO 2TB",
        ]
        suffix_map = {
            "ZHITAI Ti600 4TB": ["00C5", "0005"],
        }

        result = system_overlay._assign_unique_disk_names(names, suffix_map)

        self.assertEqual(
            result,
            [
                "ZHITAI Ti600 4TB (00C5)",
                "Samsung SSD 980 PRO 2TB",
            ],
        )

    def test_build_windows_disk_serial_suffix_map_normalizes_last_four_chars(self):
        entries = [
            {"Index": 2, "Model": "Drive A", "SerialNumber": "  0025_38BA_11B7_AB3E.  "},
            {"Index": 1, "Model": "Drive B", "SerialNumber": "ZR14X4LY"},
        ]

        result = system_overlay._build_windows_disk_serial_suffix_map(entries)

        self.assertEqual(result, {"Drive B": ["X4LY"], "Drive A": ["AB3E"]})

    def test_build_windows_disk_display_name_lookup_uses_serial_suffix_for_duplicates(self):
        entries = [
            {"Index": 1, "Model": "ZHITAI Ti600 4TB", "SerialNumber": "ZTA604TAB2522100C5"},
            {"Index": 4, "Model": "ZHITAI Ti600 4TB", "SerialNumber": "ZTA604TAB2542300005"},
            {"Index": 2, "Model": "Samsung SSD 980 PRO 2TB", "SerialNumber": "002538BA11B7AB3E"},
        ]

        result = system_overlay._build_windows_disk_display_name_lookup(entries)

        self.assertEqual(
            result,
            {
                ("ZHITAI Ti600 4TB", "index:1"): "ZHITAI Ti600 4TB (00C5)",
                ("ZHITAI Ti600 4TB", "serial:00C5"): "ZHITAI Ti600 4TB (00C5)",
                ("Samsung SSD 980 PRO 2TB", "index:2"): "Samsung SSD 980 PRO 2TB",
                ("Samsung SSD 980 PRO 2TB", "serial:AB3E"): "Samsung SSD 980 PRO 2TB",
                ("ZHITAI Ti600 4TB", "index:4"): "ZHITAI Ti600 4TB (0005)",
                ("ZHITAI Ti600 4TB", "serial:0005"): "ZHITAI Ti600 4TB (0005)",
            },
        )

    def test_resolve_disk_display_name_prefers_serial_specific_match(self):
        lookup = {
            ("ZHITAI Ti600 4TB", "index:1"): "ZHITAI Ti600 4TB (00C5)",
            ("ZHITAI Ti600 4TB", "serial:00C5"): "ZHITAI Ti600 4TB (00C5)",
            ("ZHITAI Ti600 4TB", "index:4"): "ZHITAI Ti600 4TB (0005)",
            ("ZHITAI Ti600 4TB", "serial:0005"): "ZHITAI Ti600 4TB (0005)",
        }

        first = system_overlay._resolve_disk_display_name(
            "ZHITAI Ti600 4TB",
            "ZTA604TAB2522107A0",
            1,
            lookup,
        )
        second = system_overlay._resolve_disk_display_name(
            "ZHITAI Ti600 4TB",
            "ZTA604TAB254230DV5",
            4,
            lookup,
        )

        self.assertEqual(first, "ZHITAI Ti600 4TB (07A0)")
        self.assertEqual(second, "ZHITAI Ti600 4TB (0DV5)")

    def test_assign_unique_disk_names_normalizes_whitespace_before_matching_serials(self):
        names = [
            "ZHITAI   Ti600   4TB ",
        ]
        suffix_map = {
            "ZHITAI Ti600 4TB": ["00C5", "0005"],
        }

        result = system_overlay._assign_unique_disk_names(names, suffix_map)

        self.assertEqual(result, ["ZHITAI Ti600 4TB (00C5)"])

    def test_select_best_disk_temp_sensor_scans_nested_subhardware(self):
        nested_sensor = _FakeSensor("Temperature", value=48.5)
        hardware = _FakeHardware(
            "ZHITAI Ti600 4TB",
            subhardware=[
                _FakeHardware(
                    "NVMe bridge",
                    hardware_type="Controller",
                    subhardware=[_FakeHardware("NVMe telemetry", sensors=[nested_sensor])],
                )
            ],
        )

        result = system_overlay._select_best_disk_temp_sensor(hardware)

        self.assertIs(result, nested_sensor)

    def test_refresh_lhm_storage_state_updates_duplicate_disk_sensor_cache(self):
        sensor_a = _FakeSensor("Temperature", value=46.8)
        sensor_b = _FakeSensor("Temperature", value=44.9)
        controller = _FakeHardware(
            "PCIe Controller",
            hardware_type="Controller",
            subhardware=[
                _FakeHardware(
                    "ZHITAI Ti600 4TB",
                    storage=_FakeStorage("ZTA604TAB2522107A0", 1),
                    subhardware=[_FakeHardware("Telemetry A", sensors=[sensor_a], hardware_type="Controller")],
                ),
                _FakeHardware(
                    "ZHITAI Ti600 4TB",
                    storage=_FakeStorage("ZTA604TAB254230DV5", 4),
                    subhardware=[_FakeHardware("Telemetry B", sensors=[sensor_b], hardware_type="Controller")],
                ),
            ],
        )

        system_overlay._lhm_computer = _FakeComputer([controller])
        system_overlay._lhm_disk_temps = {}
        system_overlay._lhm_disk_storage = {}
        system_overlay._lhm_disk_display_name_lookup = {
            ("ZHITAI Ti600 4TB", "index:1"): "ZHITAI Ti600 4TB (00C5)",
            ("ZHITAI Ti600 4TB", "serial:00C5"): "ZHITAI Ti600 4TB (00C5)",
            ("ZHITAI Ti600 4TB", "index:4"): "ZHITAI Ti600 4TB (0005)",
            ("ZHITAI Ti600 4TB", "serial:0005"): "ZHITAI Ti600 4TB (0005)",
        }

        system_overlay._refresh_lhm_storage_state()

        self.assertEqual(
            set(system_overlay._lhm_disk_temps.keys()),
            {"ZHITAI Ti600 4TB (07A0)", "ZHITAI Ti600 4TB (0DV5)"},
        )
        self.assertIs(system_overlay._lhm_disk_temps["ZHITAI Ti600 4TB (07A0)"], sensor_a)
        self.assertIs(system_overlay._lhm_disk_temps["ZHITAI Ti600 4TB (0DV5)"], sensor_b)

    def test_rename_disk_temp_values_applies_duplicate_suffixes_to_payload_keys(self):
        disk_values = {
            "ZHITAI Ti600 4TB": 46.85,
            "Samsung SSD 980 PRO 2TB": 40.85,
        }

        with mock.patch.object(
            system_overlay,
            "_get_preferred_disk_serial_suffix_map",
            return_value={"ZHITAI Ti600 4TB": ["07A0", "0DV5"]},
        ):
            with mock.patch.object(
                system_overlay,
                "_get_expected_disk_display_names",
                return_value=[
                    "ST8000DM004-2U9188",
                    "ZHITAI Ti600 4TB (07A0)",
                    "Samsung SSD 980 PRO 2TB",
                    "ZHITAI TiPlus7100 1TB",
                    "ZHITAI Ti600 4TB (0DV5)",
                ],
            ):
                result = system_overlay._rename_disk_temp_values(disk_values)

        self.assertEqual(
            result,
            {
                "ST8000DM004-2U9188": None,
                "ZHITAI Ti600 4TB (07A0)": 46.85,
                "Samsung SSD 980 PRO 2TB": 40.85,
                "ZHITAI TiPlus7100 1TB": None,
                "ZHITAI Ti600 4TB (0DV5)": None,
            },
        )

    def test_get_lhm_disk_serial_suffix_map_prefers_runtime_storage_serials(self):
        system_overlay._lhm_disk_storage = {
            "ZHITAI Ti600 4TB (00C5)": _FakeStorage("ZTA604TAB2522107A0", 1, "ZHITAI Ti600 4TB"),
            "ZHITAI Ti600 4TB (0005)": _FakeStorage("ZTA604TAB254230DV5", 4, "ZHITAI Ti600 4TB"),
            "Samsung SSD 980 PRO 2TB": _FakeStorage("002538BA11B7AB3E", 2, "Samsung SSD 980 PRO 2TB"),
        }

        result = system_overlay._get_lhm_disk_serial_suffix_map()

        self.assertEqual(
            result,
            {
                "ZHITAI Ti600 4TB": ["07A0", "0DV5"],
                "Samsung SSD 980 PRO 2TB": ["AB3E"],
            },
        )

    def test_get_diskinfotoolkit_temp_values_uses_serial_specific_display_names(self):
        class _ToolkitSmart:
            def __init__(self, temperature):
                self.Temperature = temperature

        class _ToolkitStorage:
            def __init__(self, model, serial_number, drive_number, temperature):
                self.Model = model
                self.SerialNumber = serial_number
                self.DriveNumber = drive_number
                self.Smart = _ToolkitSmart(temperature)

        class _ToolkitStorageManager:
            Storages = [
                _ToolkitStorage("ZHITAI Ti600 4TB", "ZTA604TAB2522107A0", 1, 46),
                _ToolkitStorage("ZHITAI Ti600 4TB", "ZTA604TAB254230DV5", 4, 44),
            ]

            @staticmethod
            def ReloadStorages():
                return None

        system_overlay._lhm_disk_display_name_lookup = {
            ("ZHITAI Ti600 4TB", "index:1"): "ZHITAI Ti600 4TB (00C5)",
            ("ZHITAI Ti600 4TB", "serial:00C5"): "ZHITAI Ti600 4TB (00C5)",
            ("ZHITAI Ti600 4TB", "index:4"): "ZHITAI Ti600 4TB (0005)",
            ("ZHITAI Ti600 4TB", "serial:0005"): "ZHITAI Ti600 4TB (0005)",
        }

        with mock.patch.object(
            system_overlay,
            "_get_diskinfotoolkit_storage_manager",
            return_value=_ToolkitStorageManager,
        ):
            result = system_overlay._get_diskinfotoolkit_temp_values()

        self.assertEqual(
            result,
            {
                "ZHITAI Ti600 4TB (07A0)": 46.0,
                "ZHITAI Ti600 4TB (0DV5)": 44.0,
            },
        )

    def test_get_expected_disk_display_names_excludes_usb_storage(self):
        entries = [
            {
                "Index": 0,
                "Model": "Disk A",
                "SerialNumber": "AAA0001",
                "InterfaceType": "SCSI",
                "MediaType": "Fixed hard disk media",
                "PNPDeviceID": "SCSI\\DISK&VEN_TEST",
            },
            {
                "Index": 1,
                "Model": "USB Disk",
                "SerialNumber": "USB0001",
                "InterfaceType": "USB",
                "MediaType": None,
                "PNPDeviceID": "USBSTOR\\DISK&VEN_TEST",
            },
        ]

        with mock.patch.object(system_overlay, "_get_windows_disk_inventory_entries", return_value=entries):
            result = system_overlay._get_expected_disk_display_names()

        self.assertEqual(result, ["Disk A"])

    def test_get_monitor_snapshot_uses_minimum_half_second_cache(self):
        with mock.patch.object(system_overlay, "get_monitor_stats", side_effect=[{"cpu_pct": 10}, {"cpu_pct": 20}]) as get_stats:
            with mock.patch.object(system_overlay.time, "monotonic", side_effect=[10.0, 10.2, 10.6]):
                with mock.patch.object(system_overlay, "datetime", _FakeDateTime):
                    first = system_overlay.get_monitor_snapshot(max_age_ms=0)
                    second = system_overlay.get_monitor_snapshot(max_age_ms=0)
                    third = system_overlay.get_monitor_snapshot(max_age_ms=0)

        self.assertEqual(get_stats.call_count, 2)
        self.assertEqual(first["stats"], {"cpu_pct": 10})
        self.assertEqual(second["stats"], {"cpu_pct": 10})
        self.assertEqual(third["stats"], {"cpu_pct": 20})
        self.assertEqual(first["timestamp"], second["timestamp"])
        self.assertNotEqual(second["timestamp"], third["timestamp"])

    def test_set_overlay_enabled_opens_overlay_and_notifies(self):
        config = {"overlay": {"enabled": False}}
        saved_states = []
        events = []

        def _save(updated_config):
            saved_states.append(updated_config["overlay"]["enabled"])

        class _FakeOverlay:
            def __init__(self, overlay_config, save_config_fn, on_state_change_fn=None):
                self.config = overlay_config
                self.save_config = save_config_fn
                self.on_state_change_fn = on_state_change_fn

            def show(self, _parent):
                if self.on_state_change_fn is not None:
                    self.on_state_change_fn(True)

        with mock.patch.object(system_overlay, "_ui_root", object()):
            with mock.patch.object(system_overlay, "_run_on_ui_thread", side_effect=lambda callback: callback()):
                with mock.patch.object(system_overlay, "SystemMonitorOverlay", _FakeOverlay):
                    system_overlay.set_overlay_enabled(
                        config,
                        _save,
                        True,
                        on_state_change_fn=events.append,
                    )

        self.assertTrue(config["overlay"]["enabled"])
        self.assertIsNotNone(system_overlay._overlay_instance)
        self.assertEqual(saved_states, [True])
        self.assertEqual(events, [True])

    def test_set_overlay_enabled_closes_overlay_and_notifies(self):
        config = {"overlay": {"enabled": True}}
        saved_states = []
        events = []

        def _save(updated_config):
            saved_states.append(updated_config["overlay"]["enabled"])

        class _FakeOverlay:
            def close(self, sync_config=True, notify_state=True):
                system_overlay._overlay_instance = None
                if sync_config and config["overlay"]["enabled"]:
                    config["overlay"]["enabled"] = False
                    _save(config)
                if notify_state:
                    events.append(False)

        system_overlay._overlay_instance = _FakeOverlay()

        with mock.patch.object(system_overlay, "_run_on_ui_thread", side_effect=lambda callback: callback()):
            system_overlay.set_overlay_enabled(
                config,
                _save,
                False,
                on_state_change_fn=events.append,
            )

        self.assertFalse(config["overlay"]["enabled"])
        self.assertIsNone(system_overlay._overlay_instance)
        self.assertEqual(saved_states, [False])
        self.assertEqual(events, [False])

    def test_close_overlay_preserves_enabled_config(self):
        config = {"overlay": {"enabled": True}}
        close_calls = []

        class _FakeOverlay:
            def close(self, sync_config=True, notify_state=True):
                close_calls.append((sync_config, notify_state))
                system_overlay._overlay_instance = None

        system_overlay._overlay_instance = _FakeOverlay()

        with mock.patch.object(system_overlay, "_run_on_ui_thread", side_effect=lambda callback: callback()):
            system_overlay.close_overlay()

        self.assertEqual(close_calls, [(False, False)])
        self.assertTrue(config["overlay"]["enabled"])


if __name__ == "__main__":
    unittest.main()