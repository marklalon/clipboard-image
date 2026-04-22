"""
Little Helper - System monitoring overlay window.

Shows RAM, CPU, GPU stats in a draggable, resizable, semi-transparent overlay.
"""

import copy
import json
import os
import subprocess
import sys
import queue
import time
import threading
import logging
from collections import Counter, defaultdict
import tkinter as tk
from tkinter import font as tkfont
from datetime import datetime, timezone

log = logging.getLogger("little_helper.system_overlay")

# --- NVML state (initialised once at startup) ---
_nvml_available = False
_nvml_handle    = None

# --- LibreHardwareMonitor state ---
_lhm_available = False
_lhm_computer  = None
_lhm_cpu_temp  = None   # ISensor reference
_lhm_cpu_power = None   # ISensor reference
_lhm_ram_temps = []     # list of ISensor references (one per DIMM)
_lhm_disk_temps = {}    # dict: {unique_disk_name: ISensor} for disk temperatures
_lhm_disk_storage = {}  # dict: {unique_disk_name: DiskInfoToolkit.Storage} for fallback
_lhm_disk_display_name_lookup = {}
_lhm_lock      = threading.Lock()  # serialises all LHM .NET object access
_ui_root       = None
_ui_thread_id  = None
_ui_tasks: queue.Queue = queue.Queue()
_snapshot_lock = threading.Lock()
_snapshot_cache = None
_snapshot_cache_at = 0.0
_MIN_SNAPSHOT_CACHE_MS = 500
_diskinfotoolkit_import_attempted = False
_diskinfotoolkit_storage_manager = None


def _disk_temp_sensor_priority(sensor_name: str) -> tuple[int, int]:
    name = (sensor_name or "").strip().lower()
    if name == "temperature":
        return (0, 0)
    if name.startswith("temperature #"):
        try:
            return (1, int(name.split("#", 1)[1].strip()))
        except (IndexError, ValueError):
            return (1, 99)
    if "warning" in name:
        return (2, 0)
    if "critical" in name:
        return (3, 0)
    return (4, 0)


def _serial_suffix(serial_number) -> str | None:
    serial = "".join(ch for ch in str(serial_number or "").upper() if ch.isalnum())
    if not serial:
        return None
    return serial[-4:] if len(serial) >= 4 else serial


def _normalize_disk_name(name) -> str:
    text = str(name or "Unknown").strip()
    # Remove trailing parentheses and their content (virtual identifiers)
    import re
    model = re.sub(r'\s*\([^)]*\)\s*$', '', text).strip()
    return " ".join(model.split()) or "Unknown"


def _build_windows_disk_serial_suffix_map(entries) -> dict[str, list[str]]:
    suffixes: dict[str, list[str]] = defaultdict(list)
    for entry in sorted(entries or [], key=lambda item: int(item.get("Index", 0))):
        model = _normalize_disk_name(entry.get("Model"))
        suffix = _serial_suffix(entry.get("SerialNumber"))
        if model and suffix:
            suffixes[model].append(suffix)
    return dict(suffixes)


def _get_lhm_disk_serial_suffix_map() -> dict[str, list[str]]:
    suffixes: dict[str, list[str]] = defaultdict(list)
    storages = sorted(
        _lhm_disk_storage.items(),
        key=lambda item: getattr(item[1], "DriveNumber", sys.maxsize),
    )
    for disk_name, storage in storages:
        model = _normalize_disk_name(getattr(storage, "Model", None) or disk_name)
        suffix = _serial_suffix(getattr(storage, "SerialNumber", None))
        if model and suffix:
            suffixes[model].append(suffix)
    return dict(suffixes)


def _get_preferred_disk_serial_suffix_map() -> dict[str, list[str]]:
    suffix_map = _get_lhm_disk_serial_suffix_map()
    if suffix_map:
        return suffix_map
    return _get_windows_disk_serial_suffix_map()


def _lookup_disk_display_names(display_name_lookup, normalized_name: str) -> set[str]:
    return {
        display_name
        for (model, lookup_key), display_name in (display_name_lookup or {}).items()
        if model == normalized_name and str(lookup_key).startswith("index:")
    }


def _get_windows_disk_inventory_entries() -> list[dict]:
    if os.name != "nt":
        return []

    try:
        import pythoncom
        import wmi

        pythoncom.CoInitialize()
        try:
            return [
                {
                    "Index": int(getattr(disk, "Index", 0) or 0),
                    "Model": getattr(disk, "Model", ""),
                    "SerialNumber": getattr(disk, "SerialNumber", ""),
                    "InterfaceType": getattr(disk, "InterfaceType", ""),
                    "MediaType": getattr(disk, "MediaType", ""),
                    "PNPDeviceID": getattr(disk, "PNPDeviceID", ""),
                }
                for disk in wmi.WMI().Win32_DiskDrive()
            ]
        finally:
            pythoncom.CoUninitialize()
    except Exception as exc:
        log.debug("Failed to query disk serial numbers via WMI: %s", exc)

    command = (
        "$ErrorActionPreference='Stop'; "
        "Get-CimInstance Win32_DiskDrive | "
        "Select-Object Index, Model, SerialNumber | "
        "ConvertTo-Json -Compress"
    )

    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = completed.stdout.strip()
        if not raw:
            return []
        payload = json.loads(raw)
        return payload if isinstance(payload, list) else [payload]
    except Exception as exc:
        log.debug("Failed to query disk serial numbers via PowerShell: %s", exc)
        return []


def _get_windows_disk_serial_suffix_map() -> dict[str, list[str]]:
    entries = _get_windows_disk_inventory_entries()
    if not entries:
        return {}
    return _build_windows_disk_serial_suffix_map(entries)


def _build_windows_disk_display_name_lookup(entries) -> dict[tuple[str, str | None], str]:
    ordered_entries = sorted(entries or [], key=lambda item: int(item.get("Index", 0)))
    display_names = _assign_unique_disk_names(
        [_normalize_disk_name(entry.get("Model")) for entry in ordered_entries],
        _build_windows_disk_serial_suffix_map(ordered_entries),
    )
    display_lookup = {}
    for entry, display_name in zip(ordered_entries, display_names):
        model = _normalize_disk_name(entry.get("Model"))
        display_lookup[(model, f"index:{int(entry.get('Index', 0))}")] = display_name
        serial_suffix = _serial_suffix(entry.get("SerialNumber"))
        if serial_suffix:
            display_lookup[(model, f"serial:{serial_suffix}")] = display_name
    return display_lookup


def _get_windows_disk_display_name_lookup() -> dict[tuple[str, str | None], str]:
    entries = _get_windows_disk_inventory_entries()
    if not entries:
        return {}
    return _build_windows_disk_display_name_lookup(entries)


def _resolve_disk_display_name(
    disk_name,
    serial_number,
    drive_number=None,
    display_name_lookup: dict[tuple[str, str | None], str] | None = None,
) -> str:
    normalized_name = _normalize_disk_name(disk_name)
    lookup = display_name_lookup or {}
    serial_suffix = _serial_suffix(serial_number)
    if serial_suffix:
        serial_key = (normalized_name, f"serial:{serial_suffix}")
        if serial_key in lookup:
            return lookup[serial_key]

        if len(_lookup_disk_display_names(lookup, normalized_name)) > 1:
            return f"{normalized_name} ({serial_suffix})"

    if drive_number is not None:
        try:
            drive_key = (normalized_name, f"index:{int(drive_number)}")
            if drive_key in lookup:
                return lookup[drive_key]
        except (TypeError, ValueError):
            pass

    return normalized_name


def _iter_hardware_tree(root_hardware):
    stack = [root_hardware]
    while stack:
        hardware = stack.pop()
        yield hardware
        try:
            stack.extend(reversed(list(hardware.SubHardware)))
        except Exception:
            pass


def _iter_storage_hardware(_lhm_computer_instance):
    if _lhm_computer_instance is None:
        return
    for hardware in _lhm_computer_instance.Hardware:
        for node in _iter_hardware_tree(hardware):
            try:
                if node.HardwareType.ToString() == "Storage":
                    yield node
            except Exception:
                continue


def _get_storage_object(hardware):
    storage_prop = hardware.GetType().GetProperty("Storage")
    return storage_prop.GetValue(hardware) if storage_prop is not None else None


def _get_disk_temp_sensor_candidates(hardware):
    candidates = []
    for node in _iter_hardware_tree(hardware):
        try:
            node.Update()
        except Exception:
            pass
        for sensor in node.Sensors:
            if sensor.SensorType.ToString() != "Temperature":
                continue
            candidates.append(sensor)
    return candidates


def _select_best_disk_temp_sensor(hardware):
    selected_sensor = None
    for sensor in _get_disk_temp_sensor_candidates(hardware):
        if selected_sensor is None or _disk_temp_sensor_priority(sensor.Name) < _disk_temp_sensor_priority(selected_sensor.Name):
            selected_sensor = sensor
    return selected_sensor


def _refresh_lhm_storage_state() -> None:
    global _lhm_disk_display_name_lookup

    if _lhm_computer is None:
        return

    if not _lhm_disk_display_name_lookup:
        _lhm_disk_display_name_lookup = _get_windows_disk_display_name_lookup()

    for hardware in _iter_storage_hardware(_lhm_computer):
        try:
            hardware.Update()
        except Exception:
            pass

        try:
            stor_obj = _get_storage_object(hardware)
        except Exception as exc:
            stor_obj = None
            log.debug("Failed to get Storage object for %s: %s", _normalize_disk_name(hardware.Name), exc)

        disk_name = _resolve_disk_display_name(
            hardware.Name,
            getattr(stor_obj, 'SerialNumber', None),
            getattr(stor_obj, 'DriveNumber', None),
            _lhm_disk_display_name_lookup,
        )

        if stor_obj is not None:
            _lhm_disk_storage[disk_name] = stor_obj

        candidates = _get_disk_temp_sensor_candidates(hardware)
        selected_sensor = None
        for sensor in candidates:
            if selected_sensor is None or _disk_temp_sensor_priority(sensor.Name) < _disk_temp_sensor_priority(selected_sensor.Name):
                selected_sensor = sensor
        if selected_sensor is not None:
            _lhm_disk_temps[disk_name] = selected_sensor


def _is_expected_disk_entry(entry: dict) -> bool:
    model = _normalize_disk_name(entry.get("Model"))
    interface_type = str(entry.get("InterfaceType") or "").strip().upper()
    media_type = str(entry.get("MediaType") or "").strip().lower()
    pnp_device_id = str(entry.get("PNPDeviceID") or "").strip().upper()

    if not model or interface_type == "USB" or pnp_device_id.startswith("USBSTOR\\"):
        return False

    if media_type and "fixed" not in media_type:
        return False

    return True


def _get_expected_disk_display_names() -> list[str]:
    entries = [
        entry for entry in _get_windows_disk_inventory_entries()
        if _is_expected_disk_entry(entry)
    ]
    entries.sort(key=lambda entry: int(entry.get("Index", 0)))
    return _assign_unique_disk_names(
        [_normalize_disk_name(entry.get("Model")) for entry in entries],
        _get_preferred_disk_serial_suffix_map(),
    )


def _assign_unique_disk_names(disk_names: list[str], serial_suffix_map: dict[str, list[str]] | None = None) -> list[str]:
    normalized_names = [_normalize_disk_name(name) for name in disk_names]
    counts = Counter(normalized_names)
    occurrences: dict[str, int] = defaultdict(int)
    serial_suffix_map = {
        _normalize_disk_name(name): list(suffixes)
        for name, suffixes in (serial_suffix_map or {}).items()
    }
    unique_names = []

    for disk_name in normalized_names:
        known_duplicates = len(serial_suffix_map.get(disk_name, [])) > 1
        if counts[disk_name] <= 1 and not known_duplicates:
            unique_names.append(disk_name)
            continue

        idx = occurrences[disk_name]
        occurrences[disk_name] += 1
        suffixes = serial_suffix_map.get(disk_name, [])
        if idx < len(suffixes):
            suffix = suffixes[idx]
        else:
            suffix = str(idx + 1)
        unique_names.append(f"{disk_name} ({suffix})")

    return unique_names


def _rename_disk_temp_values(disk_values: dict[str, float]) -> dict[str, float]:
    renamed_values: dict[str, float | None] = {
        disk_name: None for disk_name in _get_expected_disk_display_names()
    }

    if not disk_values:
        return renamed_values

    display_names = _assign_unique_disk_names(
        list(disk_values.keys()),
        _get_preferred_disk_serial_suffix_map(),
    )
    for display_name, value in zip(display_names, disk_values.values()):
        renamed_values[display_name] = value

    return renamed_values


def _get_diskinfotoolkit_storage_manager():
    global _diskinfotoolkit_import_attempted, _diskinfotoolkit_storage_manager

    if _diskinfotoolkit_import_attempted:
        return _diskinfotoolkit_storage_manager

    _diskinfotoolkit_import_attempted = True

    try:
        import clr

        if getattr(sys, 'frozen', False):
            dll_dir = os.path.join(sys._MEIPASS, "lhm")
        else:
            dll_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "lib", "lhm")

        clr.AddReference(os.path.join(dll_dir, "BlackSharp.Core.dll"))
        clr.AddReference(os.path.join(dll_dir, "DiskInfoToolkit.dll"))
        from DiskInfoToolkit import StorageManager

        _diskinfotoolkit_storage_manager = StorageManager
    except Exception as exc:
        log.debug("DiskInfoToolkit fallback unavailable: %s", exc)
        _diskinfotoolkit_storage_manager = None

    return _diskinfotoolkit_storage_manager


def _get_diskinfotoolkit_temp_values() -> dict[str, float]:
    storage_manager = _get_diskinfotoolkit_storage_manager()
    if storage_manager is None:
        return {}

    display_name_lookup = _lhm_disk_display_name_lookup or _get_windows_disk_display_name_lookup()
    disk_values = {}

    try:
        storage_manager.ReloadStorages()
        for storage in storage_manager.Storages:
            smart = getattr(storage, 'Smart', None)
            temp = getattr(smart, 'Temperature', None) if smart is not None else None
            if temp is None:
                continue
            disk_name = _resolve_disk_display_name(
                getattr(storage, 'Model', None),
                getattr(storage, 'SerialNumber', None),
                getattr(storage, 'DriveNumber', None),
                display_name_lookup,
            )
            disk_values[disk_name] = float(temp)
    except Exception as exc:
        log.debug("DiskInfoToolkit fallback read failed: %s", exc)

    return disk_values


def init_nvml() -> bool:
    """Attempt to initialise pynvml for GPU index 0. Call once at startup."""
    global _nvml_available, _nvml_handle
    # Prime psutil cpu_percent so the first background fetch returns a real value
    # (first call with interval=None always returns 0.0 unless primed)
    try:
        import psutil
        psutil.cpu_percent(interval=None)
    except Exception:
        pass
    try:
        import pynvml
        pynvml.nvmlInit()
        _nvml_handle    = pynvml.nvmlDeviceGetHandleByIndex(0)
        _nvml_available = True
        name = pynvml.nvmlDeviceGetName(_nvml_handle)
        log.info(f"NVML initialised: {name}")
        return True
    except Exception as e:
        log.warning(f"NVML init failed (no Nvidia GPU?): {e}")
        _nvml_available = False
        return False


def init_lhm() -> bool:
    """Attempt to initialise LibreHardwareMonitorLib for CPU/RAM/Disk sensors. Call once at startup."""
    global _lhm_available, _lhm_computer, _lhm_cpu_temp, _lhm_cpu_power, _lhm_ram_temps, _lhm_disk_temps, _lhm_disk_storage, _lhm_disk_display_name_lookup
    try:
        import clr
        # Find the DLL path (works for both source and PyInstaller frozen EXE)
        if getattr(sys, 'frozen', False):
            dll_dir = os.path.join(sys._MEIPASS, "lhm")
        else:
            dll_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "lib", "lhm")
        if not os.path.exists(dll_dir):
            log.debug(f"LibreHardwareMonitor DLLs not found at {dll_dir}")
            return False

        # Add reference to the DLL
        clr.AddReference(os.path.join(dll_dir, "LibreHardwareMonitorLib.dll"))
        from LibreHardwareMonitor.Hardware import Computer

        _lhm_cpu_temp = None
        _lhm_cpu_power = None
        _lhm_ram_temps = []
        _lhm_disk_temps = {}
        _lhm_disk_storage = {}
        _lhm_disk_display_name_lookup = {}

        _lhm_computer = Computer()
        _lhm_computer.IsCpuEnabled = True
        _lhm_computer.IsGpuEnabled = False
        _lhm_computer.IsMemoryEnabled = True
        _lhm_computer.IsMotherboardEnabled = True
        _lhm_computer.IsControllerEnabled = True   # needed for SMBus (DIMM temps)
        _lhm_computer.IsNetworkEnabled = False
        _lhm_computer.IsStorageEnabled = True    # needed for disk temperatures
        _lhm_computer.Open()
        _lhm_disk_display_name_lookup = _get_windows_disk_display_name_lookup()

        for hardware in _lhm_computer.Hardware:
            hw_type = hardware.HardwareType.ToString()
            hardware.Update()

            if hw_type == "Cpu":
                for sensor in hardware.Sensors:
                    sensor_type = sensor.SensorType.ToString()
                    name = sensor.Name.lower()
                    if sensor_type == "Temperature" and _lhm_cpu_temp is None:
                        if "core" in name or "package" in name or "cpu" in name:
                            _lhm_cpu_temp = sensor
                            log.debug(f"Found CPU temp sensor: {sensor.Name}")
                    elif sensor_type == "Power" and _lhm_cpu_power is None:
                        if "package" in name or "cpu" in name:
                            _lhm_cpu_power = sensor
                            log.debug(f"Found CPU power sensor: {sensor.Name}")

            elif hw_type == "Storage":
                try:
                    stor_obj = _get_storage_object(hardware)
                    disk_name = _resolve_disk_display_name(
                        hardware.Name,
                        getattr(stor_obj, 'SerialNumber', None),
                        getattr(stor_obj, 'DriveNumber', None),
                        _lhm_disk_display_name_lookup,
                    )
                    if stor_obj is not None:
                        _lhm_disk_storage[disk_name] = stor_obj
                        log.debug(
                            "Storage object for %s: serial=%s, SmartKey=%s, controller=%s",
                            disk_name,
                            getattr(stor_obj, 'SerialNumber', '?'),
                            getattr(stor_obj, 'SmartKey', '?'),
                            getattr(stor_obj, 'StorageControllerType', '?'),
                        )
                    else:
                        log.debug("Storage property is None for %s", disk_name)
                except Exception as _e:
                    disk_name = _normalize_disk_name(hardware.Name)
                    log.debug("Failed to get Storage object for %s: %s", disk_name, _e)
                
                # Always resolve disk_name for consistency, even after exception
                if 'disk_name' not in locals() or disk_name is None:
                    disk_name = _resolve_disk_display_name(
                        hardware.Name,
                        None,
                        None,
                        _lhm_disk_display_name_lookup,
                    )
                
                selected_sensor = _select_best_disk_temp_sensor(hardware)
                if selected_sensor is not None:
                    _lhm_disk_temps[disk_name] = selected_sensor
                    log.debug(
                        "Selected disk temp sensor: %s -> %s",
                        disk_name,
                        selected_sensor.Name,
                    )
                else:
                    log.debug("No temperature sensor found for disk: %s", disk_name)

            else:
                # RAM temps may appear under SMBus, EmbeddedController, or other
                # hardware types — scan all non-CPU hardware for DIMM/DDR temp sensors
                _RAM_KEYWORDS = ("ddr", "dimm", "memory", "mem ", "mem#", "channel")
                for node in list(hardware.SubHardware) + [hardware]:
                    try:
                        node.Update()
                    except Exception:
                        pass
                    for sensor in node.Sensors:
                        if sensor.SensorType.ToString() != "Temperature":
                            continue
                        name_lower = sensor.Name.lower()
                        if any(kw in name_lower for kw in _RAM_KEYWORDS):
                            _lhm_ram_temps.append(sensor)
                            log.debug(f"Found RAM temp sensor: {sensor.Name} on {hw_type}")

        _refresh_lhm_storage_state()

        _lhm_available = True
        log.info(
            f"LibreHardwareMonitorLib initialised: CPU sensors found, "
            f"{len(_lhm_ram_temps)} RAM temp sensor(s), "
            f"{len(_lhm_disk_temps)} disk sensor(s)"
        )
        return True
    except Exception as e:
        log.warning(f"LibreHardwareMonitorLib init failed: {e}")
        _lhm_available = False
        return False


def get_lhm_computer():
    """Return (computer, lock) for fan_control to share the LHM instance."""
    return _lhm_computer, _lhm_lock


def set_ui_root(root) -> None:
    """Register the shared Tk UI root used by overlay windows."""
    global _ui_root, _ui_thread_id

    def _drain_ui_tasks() -> None:
        try:
            while True:
                callback = _ui_tasks.get_nowait()
                callback()
        except queue.Empty:
            pass

        if _ui_root is not None:
            _ui_root.after(20, _drain_ui_tasks)

    _ui_root = root
    _ui_thread_id = None if root is None else threading.get_ident()
    if root is not None:
        root.after(20, _drain_ui_tasks)


def _run_on_ui_thread(callback) -> None:
    if _ui_root is None:
        raise RuntimeError("Shared Tk UI root is not available")

    if threading.get_ident() == _ui_thread_id:
        callback()
    else:
        _ui_tasks.put(callback)


def lhm_is_available() -> bool:
    return _lhm_available


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def get_gpu_stats() -> dict:
    """Return GPU metrics dict; any unavailable metric is None."""
    result = {
        "vram_used_mb":  None,
        "vram_total_mb": None,
        "gpu_util_pct":  None,
        "gpu_temp_c":    None,
        "gpu_power_w":   None,
    }
    if not _nvml_available:
        return result
    try:
        import pynvml
        h = _nvml_handle
        try:
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            result["vram_used_mb"]  = mem.used  / 1024**2
            result["vram_total_mb"] = mem.total / 1024**2
        except Exception:
            pass
        try:
            result["gpu_temp_c"] = pynvml.nvmlDeviceGetTemperature(
                h, pynvml.NVML_TEMPERATURE_GPU
            )
        except Exception:
            pass
        try:
            result["gpu_power_w"] = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
        except Exception:
            pass
        try:
            result["gpu_util_pct"] = pynvml.nvmlDeviceGetUtilizationRates(h).gpu
        except Exception:
            pass
    except Exception as e:
        log.debug(f"get_gpu_stats error: {e}")
    return result


def get_system_stats() -> dict:
    """Return system metrics dict."""
    result = {
        "ram_used_gb":  None,
        "ram_total_gb": None,
        "ram_pct":      None,
        "ram_temps":    None,   # list of temperatures for each RAM module
        "ram_temp_c":   None,   # average RAM temperature
        "disk_temps":   None,   # dict: {disk_name: temp_c}
        "cpu_pct":      None,
        "cpu_temp_c":   None,
        "cpu_power_w":  None,
    }
    try:
        import psutil
        vm = psutil.virtual_memory()
        result["ram_used_gb"]  = vm.used  / 1024**3
        result["ram_total_gb"] = vm.total / 1024**3
        result["ram_pct"]      = vm.percent
        # Use 0.1s interval for accurate measurement (blocks fetch thread briefly)
        result["cpu_pct"]      = psutil.cpu_percent(interval=0.1)

        # CPU temperature/power and RAM temps via LibreHardwareMonitorLib
        if _lhm_available and _lhm_computer is not None:
            try:
                with _lhm_lock:
                    for hardware in _lhm_computer.Hardware:
                        hw_type = hardware.HardwareType.ToString()
                        if hw_type == "Cpu":
                            hardware.Update()
                        elif hw_type == "Memory":
                            hardware.Update()
                            for sub in hardware.SubHardware:
                                try:
                                    sub.Update()
                                except Exception:
                                    pass
                    _refresh_lhm_storage_state()
                    cpu_temp  = _lhm_cpu_temp.Value  if _lhm_cpu_temp  is not None else None
                    cpu_power = _lhm_cpu_power.Value if _lhm_cpu_power is not None else None
                    ram_vals  = []
                    for s in _lhm_ram_temps:
                        try:
                            v = s.Value
                            if v is not None:
                                ram_vals.append(float(v))
                        except Exception:
                            pass
                    disk_vals = {}
                    for disk_name, sensor in _lhm_disk_temps.items():
                        try:
                            v = sensor.Value
                            if v is not None:
                                disk_vals[disk_name] = float(v)
                        except Exception:
                            pass
                    # Fallback: for drives with no LHM sensor, try Smart.Temperature directly
                    for disk_name, stor_obj in _lhm_disk_storage.items():
                        if disk_name in disk_vals:
                            continue
                        try:
                            smart = stor_obj.Smart
                            if smart is not None:
                                temp = smart.Temperature
                                if temp is not None:
                                    disk_vals[disk_name] = float(temp)
                                    log.debug("Got temp for %s via Smart.Temperature fallback: %s", disk_name, temp)
                        except Exception:
                            pass
                    missing_disk_names = [
                        disk_name for disk_name in _get_expected_disk_display_names()
                        if disk_name not in disk_vals
                    ]
                    if missing_disk_names:
                        for disk_name, temp in _get_diskinfotoolkit_temp_values().items():
                            if disk_name in missing_disk_names:
                                disk_vals[disk_name] = temp
                result["cpu_temp_c"]  = cpu_temp
                result["cpu_power_w"] = cpu_power
                if ram_vals:
                    result["ram_temps"] = ram_vals
                    result["ram_temp_c"] = sum(ram_vals) / len(ram_vals)
                result["disk_temps"] = _rename_disk_temp_values(disk_vals)
            except Exception as e:
                log.debug(f"LHM sensor read error: {e}")

    except Exception as e:
        log.error(f"get_system_stats error: {e}", exc_info=True)

    return result


def get_monitor_stats() -> dict:
    return {**get_system_stats(), **get_gpu_stats()}


def _temp_level(temp_c):
    if temp_c is None:
        return "na"
    if temp_c >= 80:
        return "hot"
    if temp_c >= 70:
        return "warm"
    return "normal"


def _level_to_color(level: str) -> str:
    if level == "hot":
        return _FG_HOT
    if level == "warm":
        return _FG_WARM
    if level == "na":
        return _FG_NA
    return _FG_NORMAL


def build_overlay_rows(stats: dict) -> dict:
    cpu_parts = []
    if stats.get("cpu_pct") is not None:
        cpu_parts.append(f"{stats['cpu_pct']:.0f}%")
    if stats.get("cpu_temp_c") is not None:
        cpu_parts.append(f"{stats['cpu_temp_c']:.0f}°C")
    if stats.get("cpu_power_w") is not None:
        cpu_parts.append(f"{stats['cpu_power_w']:.0f}W")

    ram_text = "N/A"
    ram_level = "na"
    if stats.get("ram_used_gb") is not None and stats.get("ram_total_gb") is not None:
        ram_text = f"{stats['ram_used_gb']:.1f}/{stats['ram_total_gb']:.0f}GB"
        if stats.get("ram_temp_c") is not None:
            ram_text += f"  {stats['ram_temp_c']:.0f}°C"
            ram_level = _temp_level(stats.get("ram_temp_c"))
        else:
            ram_level = "normal"

    gpu_parts = []
    if stats.get("gpu_util_pct") is not None:
        gpu_parts.append(f"{stats['gpu_util_pct']}%")
    if stats.get("gpu_temp_c") is not None:
        gpu_parts.append(f"{stats['gpu_temp_c']:.0f}°C")
    if stats.get("gpu_power_w") is not None:
        gpu_parts.append(f"{stats['gpu_power_w']:.0f}W")

    vram_text = "N/A"
    vram_level = "na"
    if stats.get("vram_used_mb") is not None and stats.get("vram_total_mb") is not None:
        vram_text = f"{stats['vram_used_mb'] / 1024:.1f}/{stats['vram_total_mb'] / 1024:.0f}GB"
        vram_level = "normal"

    cpu_level = _temp_level(stats.get("cpu_temp_c")) if stats.get("cpu_temp_c") is not None else ("normal" if cpu_parts else "na")
    gpu_level = _temp_level(stats.get("gpu_temp_c")) if stats.get("gpu_temp_c") is not None else ("normal" if gpu_parts else "na")

    return {
        "cpu": {
            "text": "  ".join(cpu_parts) if cpu_parts else "N/A",
            "level": cpu_level,
        },
        "ram": {
            "text": ram_text,
            "level": ram_level,
        },
        "gpu": {
            "text": "  ".join(gpu_parts) if gpu_parts else "N/A",
            "level": gpu_level,
        },
        "vram": {
            "text": vram_text,
            "level": vram_level,
        },
    }


def get_monitor_snapshot(max_age_ms: int = 500) -> dict:
    global _snapshot_cache, _snapshot_cache_at

    max_age_s = max(_MIN_SNAPSHOT_CACHE_MS, int(max_age_ms)) / 1000.0
    now = time.monotonic()

    with _snapshot_lock:
        if (
            _snapshot_cache is not None
            and max_age_s > 0
            and (now - _snapshot_cache_at) <= max_age_s
        ):
            return copy.deepcopy(_snapshot_cache)

        stats = get_monitor_stats()
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "sources": {
                "nvml": _nvml_available,
                "lhm": _lhm_available,
            },
            "stats": stats,
        }
        _snapshot_cache = snapshot
        _snapshot_cache_at = now
        return copy.deepcopy(snapshot)


# ---------------------------------------------------------------------------
# Overlay window
# ---------------------------------------------------------------------------

_BG        = "#1a1a1a"
_TITLE_BG  = "#252525"
_FG_NORMAL = "#00e676"
_FG_WARM   = "#ffdd00"
_FG_HOT    = "#ff4444"
_FG_NA     = "#777777"
_FONT      = ("Consolas", 9)
_FONT_BOLD = ("Consolas", 9, "bold")


def _temp_color(temp_c):
    return _level_to_color(_temp_level(temp_c))


def _fmt(val, fmt, unit="", na="N/A"):
    if val is None:
        return na
    return f"{val:{fmt}}{unit}"


class SystemMonitorOverlay:
    """
    Semi-transparent always-on-top overlay.
    Lives on the shared Tk UI thread as a Toplevel window.
    """

    def __init__(self, config: dict, save_config_fn, on_close_fn=None):
        self.config        = config
        self.save_config   = save_config_fn
        self._on_close_fn  = on_close_fn
        self._running      = False
        self._fetch_running = False
        self._q: queue.Queue = queue.Queue(maxsize=1)

        # drag state
        self._drag_offset_x = 0
        self._drag_offset_y = 0
        self._closed = False

        self.root   = None
        self._labels = {}  # key -> tk.Label

    # -----------------------------------------------------------------------
    # Public API (called from other threads)
    # -----------------------------------------------------------------------

    def show(self, parent) -> None:
        """Build the overlay window on the shared Tk UI thread."""
        global _overlay_instance
        self._running = True
        self.root = tk.Toplevel(parent)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.config["overlay"]["opacity"])
        self.root.configure(bg=_BG)
        self.root.resizable(False, False)
        self.root.bind("<Destroy>", self._on_destroy, add="+")

        self._build_ui()
        self._position_window()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        # Kick off stats loop
        self.root.after(100, self._update_stats)

    def close(self) -> None:
        """Destroy the window (safe to call from any thread)."""
        def _close_impl():
            self._finalize_close()
            if self.root is not None:
                try:
                    if self.root.winfo_exists():
                        self.root.destroy()
                except Exception:
                    pass

        if self.root is not None:
            try:
                _run_on_ui_thread(_close_impl)
            except Exception:
                pass
        else:
            self._finalize_close()

    def _finalize_close(self) -> None:
        global _overlay_instance

        if self._closed:
            return

        self._closed = True
        self._running = False
        if _overlay_instance is self:
            _overlay_instance = None
        if self._on_close_fn:
            try:
                self._on_close_fn()
            except Exception:
                pass

    def _on_destroy(self, event) -> None:
        if event.widget is self.root:
            self._finalize_close()

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = self.root

        # ── Title bar ──────────────────────────────────────────────────────
        self._title_bar = tk.Frame(root, bg=_TITLE_BG, height=22, cursor="fleur")
        self._title_bar.pack(fill="x", side="top")
        self._title_bar.pack_propagate(False)

        tk.Label(
            self._title_bar, text="◈ MONITOR", bg=_TITLE_BG,
            fg=_FG_NA, font=_FONT_BOLD, anchor="w",
        ).pack(side="left", padx=6)

        self._close_btn = tk.Label(
            self._title_bar, text="[×]", bg=_TITLE_BG,
            fg=_FG_NA, font=_FONT, cursor="hand2",
        )
        self._close_btn.pack(side="right", padx=4)
        self._close_btn.bind("<Button-1>", lambda e: self.close())

        # Drag bindings on title bar (skip compact button so it keeps its click handler)
        self._title_bar.bind("<ButtonPress-1>",   self._drag_start)
        self._title_bar.bind("<B1-Motion>",        self._drag_motion)
        self._title_bar.bind("<ButtonRelease-1>",  self._drag_stop)
        for child in self._title_bar.winfo_children():
            if child is self._close_btn:
                continue
            child.bind("<ButtonPress-1>",  self._drag_start)
            child.bind("<B1-Motion>",       self._drag_motion)
            child.bind("<ButtonRelease-1>", self._drag_stop)

        # ── Content frame ─────────────────────────────────────────────────
        self._content = tk.Frame(root, bg=_BG)
        self._content.pack(fill="both", expand=True)

        # System section
        self._sys_frame = tk.Frame(self._content, bg=_BG)
        self._sys_frame.pack(fill="x", padx=6, pady=(4, 2))

        self._make_row(self._sys_frame, "cpu", "CPU")
        self._make_row(self._sys_frame, "ram", "RAM")

        tk.Frame(self._content, bg="#333333", height=1).pack(fill="x", padx=6, pady=2)

        # GPU section
        self._gpu_frame = tk.Frame(self._content, bg=_BG)
        self._gpu_frame.pack(fill="x", padx=6, pady=(2, 4))

        self._make_row(self._gpu_frame, "gpu",  "GPU")
        self._make_row(self._gpu_frame, "vram", "VRAM")


    def _make_row(self, parent, key: str, label: str) -> None:
        row = tk.Frame(parent, bg=_BG)
        row.pack(fill="x", pady=1)
        tk.Label(row, text=f"{label:<4}", bg=_BG, fg=_FG_NA,
                 font=_FONT, width=4, anchor="w").pack(side="left")
        lbl = tk.Label(row, text="...", bg=_BG, fg=_FG_NORMAL,
                       font=_FONT, anchor="w")
        lbl.pack(side="left", fill="x", expand=True)
        self._labels[key] = lbl

    # -----------------------------------------------------------------------
    # Stats update (queue-based, non-blocking UI)
    # -----------------------------------------------------------------------

    def _update_stats(self) -> None:
        if not self._running:
            return

        try:
            # Check if the underlying window still exists (OS may have destroyed it)
            if not self.root.winfo_exists():
                log.warning("Overlay window was destroyed externally, cleaning up")
                self.close()
                return

            # Drain queue
            try:
                stats = self._q.get_nowait()
                self._apply_stats(stats)
            except queue.Empty:
                pass

            # Spawn fetch thread if idle
            if not self._fetch_running:
                self._fetch_running = True
                threading.Thread(target=self._fetch_thread, daemon=True).start()

            # Re-assert topmost every 30 cycles (~30s at 1000ms refresh) to
            # counteract Windows DWM occasionally dropping the flag.
            self._topmost_counter = getattr(self, "_topmost_counter", 0) + 1
            if self._topmost_counter >= 30:
                self._topmost_counter = 0
                self.root.attributes("-topmost", False)
                self.root.attributes("-topmost", True)

        except Exception:
            log.exception("Error in overlay update loop")

        # Always reschedule as long as we're running, even if an error occurred
        if self._running:
            refresh = self.config["overlay"].get("refresh_ms", 1000)
            self.root.after(refresh, self._update_stats)

    def _fetch_thread(self) -> None:
        try:
            snapshot = get_monitor_snapshot()
            try:
                self._q.put_nowait(snapshot)
            except queue.Full:
                pass
        finally:
            self._fetch_running = False

    def _apply_stats(self, snapshot: dict) -> None:
        """Update label text and colours from a monitor snapshot."""
        rows = snapshot.get("overlay") or build_overlay_rows(snapshot.get("stats", snapshot))
        for key, label in self._labels.items():
            row = rows.get(key, {"text": "N/A", "level": "na"})
            self._set(label, row["text"], _level_to_color(row["level"]))

    @staticmethod
    def _set(label: tk.Label, text: str, color: str) -> None:
        label.configure(text=text, fg=color)

    # -----------------------------------------------------------------------
    # Drag to move
    # -----------------------------------------------------------------------

    def _drag_start(self, event) -> None:
        self._drag_offset_x = event.x_root - self.root.winfo_x()
        self._drag_offset_y = event.y_root - self.root.winfo_y()

    def _drag_motion(self, event) -> None:
        x = event.x_root - self._drag_offset_x
        y = event.y_root - self._drag_offset_y
        self.root.geometry(f"+{x}+{y}")

    def _drag_stop(self, event) -> None:
        self._save_position()

    # -----------------------------------------------------------------------
    # Position helpers
    # -----------------------------------------------------------------------

    def _position_window(self) -> None:
        cfg = self.config["overlay"]
        self.root.update_idletasks()  # allow content to determine natural size
        if cfg["x"] == -1 or cfg["y"] == -1:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            w  = self.root.winfo_reqwidth() or 210
            h  = self.root.winfo_reqheight() or 120
            x  = sw - w - 10
            y  = sh - h - 50  # Account for taskbar
        else:
            x, y = cfg["x"], cfg["y"]
        self.root.geometry(f"+{x}+{y}")

    def _save_position(self) -> None:
        self.config["overlay"]["x"] = self.root.winfo_x()
        self.config["overlay"]["y"] = self.root.winfo_y()
        self.save_config(self.config)




# ---------------------------------------------------------------------------
# Module-level toggle helper (called from tray menu)
# ---------------------------------------------------------------------------

_overlay_instance: SystemMonitorOverlay | None = None


def toggle_overlay(config: dict, save_config_fn, on_close_fn=None) -> None:
    """Show or hide the overlay. Safe to call from any thread."""
    def _toggle_impl():
        global _overlay_instance

        if _overlay_instance is not None:
            _overlay_instance.close()
        else:
            _overlay_instance = SystemMonitorOverlay(config, save_config_fn, on_close_fn=on_close_fn)
            _overlay_instance.show(_ui_root)

    try:
        _run_on_ui_thread(_toggle_impl)
    except Exception as e:
        log.error(f"Error toggling overlay: {e}", exc_info=True)


def close_overlay() -> None:
    """Close overlay if open (called during shutdown)."""
    def _close_impl():
        if _overlay_instance is not None:
            _overlay_instance.close()

    try:
        _run_on_ui_thread(_close_impl)
    except Exception:
        if _overlay_instance is not None:
            _overlay_instance.close()


def overlay_is_open() -> bool:
    return _overlay_instance is not None


def apply_overlay_opacity(opacity: float) -> None:
    """Apply opacity to the running overlay immediately (no restart needed)."""
    def _apply_impl():
        if _overlay_instance is not None and _overlay_instance.root is not None:
            try:
                _overlay_instance.root.attributes("-alpha", opacity)
            except Exception:
                pass

    try:
        _run_on_ui_thread(_apply_impl)
    except Exception:
        pass

