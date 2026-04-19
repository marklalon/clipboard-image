"""
Little Helper - Auto Sleep functionality.

Monitors system utilization (CPU, GPU, disk) and user activity.
When idle for N minutes with low resource usage, shows a countdown
and triggers system sleep (S3 suspend).
"""

import ctypes
import ctypes.wintypes
import math
import threading
import logging
import time
from collections import deque

log = logging.getLogger("little_helper.auto_sleep")

_COUNTDOWN_BG = "#111111"
_COUNTDOWN_TITLE_BG = "#252525"
_COUNTDOWN_TEXT = "#ffdd00"
_COUNTDOWN_TEXT_DIM = "#b8a800"
_COUNTDOWN_TEXT_MUTED = "#777777"
_COUNTDOWN_FONT = ("Consolas", 10)
_COUNTDOWN_FONT_BOLD = ("Consolas", 10, "bold")
_COUNTDOWN_NUMBER_FONT = ("Consolas", 44, "bold")

# --- Win32 constants ---
LASTINPUTINFO_SIZE = 8  # struct size in bytes (4 bytes padding + 4 bytes tick count)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint),
    ]


# --- Module state ---
_stop_event = threading.Event()
_thread: threading.Thread | None = None
_keyboard_activity_time = 0.0  # timestamp of last keyboard activity
_mouse_activity_time = 0.0     # timestamp of last mouse activity
_last_mouse_pos = None
_samples = deque()  # tuples of (timestamp, cpu_pct, gpu_pct, disk_mbps)
_lock = threading.Lock()
_ui_callback = None  # callback to create Tkinter countdown window on UI thread

# Check interval (seconds)
CHECK_INTERVAL_S = 5

# Previous disk I/O counters for computing delta
_last_disk_io = None
_last_disk_io_time = None


def notify_keyboard_activity() -> None:
    """Called by hotkey.py when any keyboard input is detected."""
    global _keyboard_activity_time
    with _lock:
        _keyboard_activity_time = time.time()


def set_ui_callback(callback) -> None:
    """Set the callback function to create Tkinter countdown windows on the UI thread."""
    global _ui_callback
    _ui_callback = callback


def _get_disk_mbps() -> float:
    """Compute disk read+write throughput in MB/s since last call."""
    global _last_disk_io, _last_disk_io_time
    try:
        import psutil
        now = time.monotonic()
        curr_io = psutil.disk_io_counters(perdisk=False)
        
        if _last_disk_io is None or _last_disk_io_time is None:
            # First call, initialize
            _last_disk_io = curr_io
            _last_disk_io_time = now
            return 0.0
        
        time_delta = now - _last_disk_io_time
        if time_delta < 0.1:
            return 0.0
        
        bytes_delta = (curr_io.read_bytes - _last_disk_io.read_bytes) + \
                      (curr_io.write_bytes - _last_disk_io.write_bytes)
        mbps = bytes_delta / time_delta / 1e6
        
        _last_disk_io = curr_io
        _last_disk_io_time = now
        return mbps
    except Exception as e:
        log.debug(f"Error computing disk MB/s: {e}")
        return 0.0


def _check_user_input_since(tick_count: int) -> bool:
    """Check if user input occurred since given tick count (via GetLastInputInfo)."""
    try:
        user32 = ctypes.windll.user32
        info = LASTINPUTINFO()
        info.cbSize = LASTINPUTINFO_SIZE
        if user32.GetLastInputInfo(ctypes.byref(info)):
            return info.dwTime > tick_count
    except Exception as e:
        log.debug(f"Error in GetLastInputInfo: {e}")
    return False


def _get_last_input_tick() -> int:
    """Get the tick count of last user input (GetLastInputInfo)."""
    try:
        user32 = ctypes.windll.user32
        info = LASTINPUTINFO()
        info.cbSize = LASTINPUTINFO_SIZE
        if user32.GetLastInputInfo(ctypes.byref(info)):
            return info.dwTime
    except Exception as e:
        log.debug(f"Error in GetLastInputInfo: {e}")
    return 0


def _trigger_sleep() -> None:
    """Trigger system sleep (S3 suspend)."""
    try:
        ctypes.windll.PowrProf.SetSuspendState(False, True, False)
        log.info("System sleep triggered")
    except Exception as e:
        log.error(f"Error triggering sleep: {e}")


def _show_countdown_window(
    seconds: int,
    cancel_event: threading.Event,
    completed_event: threading.Event,
) -> None:
    """
    Create and show a Tkinter countdown window.
    This function should be called from the UI thread via _ui_callback.
    """
    try:
        import tkinter as tk
    except ImportError:
        log.error("tkinter unavailable for countdown window")
        cancel_event.wait(seconds)
        return

    countdown_root = tk.Toplevel()
    countdown_root.title("Little Helper - Auto Sleep")
    countdown_root.overrideredirect(True)
    countdown_root.attributes("-topmost", True)
    countdown_root.attributes("-alpha", 0.84)
    countdown_root.resizable(False, False)
    countdown_root.lift()
    countdown_root.configure(bg=_COUNTDOWN_BG)
    
    # Window appearance
    width = 420
    height = 210
    countdown_root.geometry(f"{width}x{height}")
    countdown_root.update_idletasks()
    x = countdown_root.winfo_screenwidth() // 2 - width // 2
    y = countdown_root.winfo_screenheight() // 2 - height // 2
    countdown_root.geometry(f"+{x}+{y}")
    try:
        countdown_root.focus_force()
    except Exception:
        pass

    drag_state = {"x": 0, "y": 0}

    def _drag_start(event) -> None:
        drag_state["x"] = event.x_root - countdown_root.winfo_x()
        drag_state["y"] = event.y_root - countdown_root.winfo_y()

    def _drag_motion(event) -> None:
        next_x = event.x_root - drag_state["x"]
        next_y = event.y_root - drag_state["y"]
        countdown_root.geometry(f"+{next_x}+{next_y}")

    outer = tk.Frame(
        countdown_root,
        bg=_COUNTDOWN_BG,
        highlightbackground=_COUNTDOWN_TITLE_BG,
        highlightthickness=1,
        bd=0,
    )
    outer.pack(fill="both", expand=True)

    title_bar = tk.Frame(outer, bg=_COUNTDOWN_TITLE_BG, height=22, cursor="fleur")
    title_bar.pack(fill="x", side="top")
    title_bar.pack_propagate(False)

    title_label = tk.Label(
        title_bar,
        text="◈ AUTO SLEEP",
        bg=_COUNTDOWN_TITLE_BG,
        fg=_COUNTDOWN_TEXT_MUTED,
        font=_COUNTDOWN_FONT_BOLD,
        anchor="w",
    )
    title_label.pack(side="left", padx=6)

    msg_frame = tk.Frame(outer, bg=_COUNTDOWN_BG, padx=20, pady=18)
    msg_frame.pack(fill="both", expand=True)

    tk.Label(
        msg_frame,
        text="System will sleep in:",
        font=_COUNTDOWN_FONT_BOLD,
        bg=_COUNTDOWN_BG,
        fg=_COUNTDOWN_TEXT,
        justify="center",
    ).pack(pady=10)

    countdown_var = tk.StringVar(value=str(seconds))
    tk.Label(
        msg_frame,
        textvariable=countdown_var,
        font=_COUNTDOWN_NUMBER_FONT,
        bg=_COUNTDOWN_BG,
        fg=_COUNTDOWN_TEXT,
        justify="center",
    ).pack(pady=10)

    tk.Label(
        msg_frame,
        text="(Press any key or move mouse to cancel)",
        font=_COUNTDOWN_FONT,
        bg=_COUNTDOWN_BG,
        fg=_COUNTDOWN_TEXT_DIM,
        justify="center",
    ).pack(pady=5)

    state = {"closed": False}

    def _destroy_window() -> None:
        if state["closed"]:
            return
        state["closed"] = True
        try:
            countdown_root.destroy()
        except Exception:
            pass

    def _cancel_countdown() -> None:
        cancel_event.set()
        _destroy_window()

    countdown_root.protocol("WM_DELETE_WINDOW", _cancel_countdown)

    deadline = time.monotonic() + seconds

    def _update_countdown() -> None:
        if state["closed"]:
            return

        if cancel_event.is_set():
            log.debug("Countdown cancelled (cancel_event set)")
            _destroy_window()
            return

        remaining = max(0, math.ceil(deadline - time.monotonic()))
        countdown_var.set(str(remaining))

        if remaining <= 0:
            log.info("Countdown reached 0, setting completed_event")
            completed_event.set()
            _destroy_window()
            return

        countdown_root.after(100, _update_countdown)

    countdown_root.after(0, _update_countdown)


def _create_countdown_session(seconds: int) -> tuple[threading.Event, threading.Event] | None:
    """Create the Tk countdown window on the UI thread and return its state events."""
    if _ui_callback is None:
        log.warning("UI callback not set, cannot show countdown window")
        return None

    cancel_event = threading.Event()
    completed_event = threading.Event()

    try:
        _ui_callback(lambda: _show_countdown_window(seconds, cancel_event, completed_event))
    except Exception as e:
        log.error(f"Error creating countdown window: {e}")
        return None

    return cancel_event, completed_event


def _wait_for_countdown_result(
    countdown_secs: int,
    cancel_event: threading.Event,
    completed_event: threading.Event,
    cancel_log_message: str,
    stop_event: threading.Event | None = None,
) -> bool:
    """Wait for countdown completion while watching for user input cancellation."""
    initial_input_tick = _get_last_input_tick()
    deadline = time.monotonic() + countdown_secs
    log.debug(f"Countdown started, initial_input_tick={initial_input_tick}, deadline in {countdown_secs}s")

    while time.monotonic() < deadline:
        if completed_event.is_set():
            log.info("Countdown: completed_event is set, returning True")
            return True

        if cancel_event.is_set():
            log.info(cancel_log_message)
            return False

        if _check_user_input_since(initial_input_tick):
            log.info(f"{cancel_log_message} (user input detected)")
            cancel_event.set()
            return False

        if stop_event is not None and stop_event.is_set():
            log.info("Countdown cancelled (stop_event set)")
            cancel_event.set()
            return False

        time.sleep(0.1)

    remaining_time = time.monotonic() - deadline
    result = completed_event.is_set()
    log.info(f"Countdown deadline reached (overrun={remaining_time:.1f}s), completed_event={result}")
    return result


def _do_countdown(config: dict) -> None:
    """
    Show countdown window and wait for it to complete or be cancelled.
    If completed without cancellation, triggers system sleep.
    """
    global _keyboard_activity_time, _mouse_activity_time
    
    countdown_secs = config["auto_sleep"]["countdown_seconds"]
    session = _create_countdown_session(countdown_secs)
    if session is None:
        return
    cancel_event, completed_event = session

    completed = _wait_for_countdown_result(
        countdown_secs,
        cancel_event,
        completed_event,
        cancel_log_message="User input detected during countdown, cancelling sleep",
        stop_event=_stop_event,
    )

    if not completed:
        # Countdown was cancelled; reset activity times so idle detection waits a full cycle
        now = time.time()
        _keyboard_activity_time = now
        _mouse_activity_time = now
        return

    log.info("Countdown completed, triggering sleep")
    _trigger_sleep()


def _monitor_loop(config: dict) -> None:
    """
    Main monitoring loop: runs every CHECK_INTERVAL_S seconds.
    Collects stats, detects idle conditions, triggers countdown if needed.
    """
    global _last_mouse_pos, _last_disk_io, _last_disk_io_time, _keyboard_activity_time, _mouse_activity_time
    
    try:
        import psutil
    except ImportError:
        log.error("psutil required for auto_sleep")
        return
    
    try:
        from system_overlay import get_gpu_stats
    except ImportError:
        log.error("system_overlay import failed")
        return
    
    import win32api
    
    while not _stop_event.is_set():
        try:
            # Collect system stats
            cpu_pct = psutil.cpu_percent(interval=0.1)
            gpu_stats = get_gpu_stats()
            gpu_pct = gpu_stats.get("gpu_util_pct") or 0
            disk_mbps = _get_disk_mbps()
            
            # Track mouse movement
            try:
                curr_pos = win32api.GetCursorPos()
                with _lock:
                    if _last_mouse_pos is not None and _last_mouse_pos != curr_pos:
                        _mouse_activity_time = time.time()
                    _last_mouse_pos = curr_pos
            except Exception as e:
                log.debug(f"Error getting mouse position: {e}")
            
            # Add sample to deque
            now = time.time()
            sample = (now, cpu_pct, gpu_pct, disk_mbps)
            
            idle_triggered = False
            with _lock:
                _samples.append(sample)
                
                # Trim samples older than idle_seconds
                idle_seconds = config["auto_sleep"]["idle_seconds"]
                cutoff_time = now - idle_seconds
                while _samples and _samples[0][0] < cutoff_time:
                    _samples.popleft()
                
                # Check idle condition
                if len(_samples) > 0:
                    cpu_threshold = config["auto_sleep"]["cpu_threshold"]
                    gpu_threshold = config["auto_sleep"]["gpu_threshold"]
                    disk_threshold = config["auto_sleep"]["disk_threshold_mbps"]
                    
                    # All samples must be below thresholds
                    all_below = all(
                        (cpu <= cpu_threshold and gpu <= gpu_threshold and disk <= disk_threshold)
                        for (_, cpu, gpu, disk) in _samples
                    )
                    
                    # Check if both keyboard and mouse have been inactive for the entire idle period
                    idle_since_keyboard = now - _keyboard_activity_time >= idle_seconds
                    idle_since_mouse = now - _mouse_activity_time >= idle_seconds
                    
                    if all_below and idle_since_keyboard and idle_since_mouse:
                        log.info(
                            f"Idle detected: CPU={cpu_pct:.1f}% "
                            f"GPU={gpu_pct:.1f}% Disk={disk_mbps:.1f}MB/s; "
                            f"keyboard and mouse both inactive for {idle_seconds}s"
                        )
                        _samples.clear()
                        _last_mouse_pos = None
                        _last_disk_io = None
                        _last_disk_io_time = None
                        idle_triggered = True
            
            # Call countdown outside the lock to avoid deadlock
            if idle_triggered:
                _do_countdown(config)
        
        except Exception as e:
            log.error(f"Error in monitor loop: {e}", exc_info=True)
            # Continue running despite errors
        
        # Wait for next check interval
        _stop_event.wait(CHECK_INTERVAL_S)


def start_auto_sleep(config: dict) -> None:
    """Start the auto_sleep monitoring thread."""
    global _thread, _stop_event, _keyboard_activity_time, _mouse_activity_time, _samples, _last_disk_io, _last_disk_io_time
    
    if not config.get("auto_sleep", {}).get("enabled", False):
        log.info("Auto sleep disabled, not starting")
        return
    
    # Reset state
    _stop_event.clear()
    # Initialize activity times to now so we don't trigger idle immediately
    now = time.time()
    _keyboard_activity_time = now
    _mouse_activity_time = now
    _samples.clear()
    _last_disk_io = None
    _last_disk_io_time = None
    
    _thread = threading.Thread(
        target=_monitor_loop,
        args=(config,),
        daemon=True,
        name="auto-sleep-monitor",
    )
    _thread.start()
    log.info("Auto sleep monitor started")


def stop_auto_sleep() -> None:
    """Stop the auto_sleep monitoring thread."""
    global _thread, _stop_event
    
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)
        _thread = None
    log.info("Auto sleep monitor stopped")


def is_auto_sleep_active() -> bool:
    """Check if auto_sleep monitoring is running."""
    return _thread is not None and _thread.is_alive()


def test_countdown_window(countdown_secs: int = 10) -> None:
    """
    Force display a countdown window for testing purposes.
    Does not actually trigger sleep, just shows the countdown UI.
    """
    def _run_test_countdown() -> None:
        session = _create_countdown_session(countdown_secs)
        if session is None:
            return

        cancel_event, completed_event = session
        completed = _wait_for_countdown_result(
            countdown_secs,
            cancel_event,
            completed_event,
            cancel_log_message="Test countdown cancelled by user input",
        )

        if completed:
            log.info("Test countdown completed")

    threading.Thread(
        target=_run_test_countdown,
        daemon=True,
        name="auto-sleep-test-countdown",
    ).start()
