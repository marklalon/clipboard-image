"""
Clipboard Image Paster - System Tray Tool
Monitors Ctrl+V and saves clipboard images as files to the active Explorer window's directory.
Uses low-level Windows keyboard hook (no admin required).
"""

import os
import sys
import ctypes
import ctypes.wintypes

# Enable DPI awareness to prevent screenshot scaling issues
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    pass  # May fail on older Windows versions

import threading
import logging
import urllib.parse
from datetime import datetime
from io import BytesIO

import tkinter as tk
import pystray
import pythoncom
import win32gui
import win32com.client
import win32clipboard
import win32con
from PIL import Image, ImageGrab, ImageTk

# --- Logging setup ---
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clipboard_image.log")

# Clear log file on startup
if os.path.exists(LOG_PATH):
    open(LOG_PATH, "w", encoding="utf-8").close()

# Set up root logger to avoid Pillow debug noise
logging.getLogger().setLevel(logging.WARNING)

# Set up our logger
log = logging.getLogger("clipboard_image")
log.setLevel(logging.DEBUG)
log.propagate = False

file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(file_handler)

# Also log to stderr when running from console
if sys.stderr and hasattr(sys.stderr, "write"):
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.DEBUG)
    stderr_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(stderr_handler)


def get_script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def get_explorer_path():
    """Get the directory path of the currently focused Explorer window."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        log.debug("No foreground window")
        return None

    class_name = win32gui.GetClassName(hwnd)
    log.debug(f"Foreground window class: {class_name}, hwnd: {hwnd}")

    # Desktop window (WorkerW or Progman) -> use Desktop path
    if class_name in ("WorkerW", "Progman"):
        desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
        if os.path.isdir(desktop):
            log.debug(f"Desktop detected, path: {desktop}")
            return desktop
        log.debug("Desktop detected but path not found")
        return None

    # Only proceed if it's an Explorer window
    if class_name not in ("CabinetWClass", "ExploreWClass"):
        log.debug("Not an Explorer window, skipping")
        return None

    try:
        pythoncom.CoInitialize()
        shell = win32com.client.Dispatch("Shell.Application")
        windows = shell.Windows()
        log.debug(f"Shell.Windows count: {windows.Count}")

        for i in range(windows.Count):
            try:
                window = windows.Item(i)
                if window is None:
                    continue
                if window.HWND == hwnd:
                    url = window.LocationURL
                    log.debug(f"Matched window, LocationURL: {url}")
                    if url and url.startswith("file:///"):
                        path = urllib.parse.unquote(url[8:]).replace("/", "\\")
                        if os.path.isdir(path):
                            return path
                    # Fallback
                    folder_path = window.Document.Folder.Self.Path
                    log.debug(f"Fallback folder path: {folder_path}")
                    if os.path.isdir(folder_path):
                        return folder_path
            except Exception as e:
                log.debug(f"Error checking window {i}: {e}")
                continue
    except Exception as e:
        log.error(f"COM error in get_explorer_path: {e}")
    finally:
        pythoncom.CoUninitialize()

    return None


def generate_filename(directory):
    """Generate a unique filename with clipboard- prefix."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"clipboard-{timestamp}"
    path = os.path.join(directory, f"{base}.png")
    if not os.path.exists(path):
        return path
    counter = 1
    while True:
        path = os.path.join(directory, f"{base}-{counter}.png")
        if not os.path.exists(path):
            return path
        counter += 1


# Global reference to tray icon for notifications
_tray_icon = None


def get_clipboard_image():
    """Get image from clipboard. Returns PIL Image or None."""
    img = ImageGrab.grabclipboard()
    if isinstance(img, Image.Image):
        log.debug("Got image via ImageGrab.grabclipboard()")
        return img
    if isinstance(img, list):
        # List of file paths - try to open as images
        for path in img:
            try:
                return Image.open(path)
            except Exception:
                pass
    return None


def on_paste():
    """Handle Ctrl+V: save clipboard image to Explorer directory."""
    log.debug("on_paste triggered")
    try:
        img = get_clipboard_image()
        log.debug(f"Clipboard content type: {type(img)}")

        if img is None:
            log.debug("Clipboard does not contain an image, passing through")
            return

        target_dir = get_explorer_path()
        log.debug(f"Target directory: {target_dir}")
        if not target_dir:
            return

        filepath = generate_filename(target_dir)
        img.save(filepath, "PNG")
        log.info(f"Saved clipboard image to: {filepath}")

    except Exception as e:
        log.error(f"Error in on_paste: {e}", exc_info=True)


def copy_image_to_clipboard(img):
    """Copy a PIL Image to the Windows clipboard."""
    if img.mode != "RGB":
        img = img.convert("RGB")

    output = BytesIO()
    try:
        # PIL writes a valid BMP including correct row padding.
        img.save(output, "BMP")
        dib_data = output.getvalue()[14:]
        log.debug(f"Prepared CF_DIB payload: {len(dib_data)} bytes")
    finally:
        output.close()

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, dib_data)
        log.info("Image copied to clipboard")
    finally:
        win32clipboard.CloseClipboard()


def on_screenshot():
    """Handle Alt+A: launch screenshot selection mode."""
    log.debug("on_screenshot triggered")
    try:
        selector = ScreenshotSelector()
        selector.run()
    except Exception as e:
        log.error(f"Error in on_screenshot: {e}", exc_info=True)


class ScreenshotSelector:
    """Fullscreen screenshot selector with area selection."""

    def __init__(self):
        self.root = None
        self.canvas = None
        self.screenshot = None
        self.photo = None
        self.start_x = None
        self.start_y = None
        self.rect_id = None
        self.selection_box = None
        self.pending_start = None
        self.dragging_selection = False

    def run(self):
        """Run the screenshot selector."""
        # Capture the full screen
        self.screenshot = ImageGrab.grab()
        screen_width, screen_height = self.screenshot.size

        # Create fullscreen window
        self.root = tk.Tk()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.config(bg="black")

        # Create canvas to display screenshot
        self.canvas = tk.Canvas(
            self.root,
            width=screen_width,
            height=screen_height,
            bg="black",
            highlightthickness=0,
        )
        self.canvas.pack()

        # Display screenshot
        self.photo = ImageTk.PhotoImage(self.screenshot)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        # Draw border to indicate screenshot mode
        border_width = 4
        border_color = "#FF8C00"
        for x1, y1, x2, y2 in [
            (0, 0, screen_width, border_width),
            (0, screen_height - border_width, screen_width, screen_height),
            (0, 0, border_width, screen_height),
            (screen_width - border_width, 0, screen_width, screen_height),
        ]:
            self.canvas.create_rectangle(x1, y1, x2, y2, fill=border_color, outline="")

        # Bind events
        self.canvas.bind("<ButtonPress-1>", self.on_mouse_press)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<Double-Button-1>", self.on_double_click)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_release)
        self.canvas.bind("<Button-3>", self.on_right_click)
        self.root.bind("<Escape>", self.on_escape)

        # Run the main loop
        self.root.mainloop()

    def on_mouse_press(self, event):
        """Record start position on mouse press."""
        # Keep the current selection intact so a confirmation double-click
        # does not reset it to a tiny box on the first click.
        if self.selection_box is not None:
            self.pending_start = (event.x, event.y)
            self.dragging_selection = False
            return

        self.start_x = event.x
        self.start_y = event.y
        self.pending_start = None
        self.dragging_selection = True
        self.selection_box = (event.x, event.y, event.x, event.y)

    def on_mouse_drag(self, event):
        """Update selection rectangle on mouse drag."""
        if self.pending_start is not None and not self.dragging_selection:
            self.start_x, self.start_y = self.pending_start
            self.pending_start = None
            self.dragging_selection = True
            self.selection_box = (self.start_x, self.start_y, event.x, event.y)

        if self.start_x is None:
            return

        # Delete previous rectangle
        if self.rect_id:
            self.canvas.delete(self.rect_id)

        # Draw new rectangle (gray-white color)
        self.rect_id = self.canvas.create_rectangle(
            self.start_x,
            self.start_y,
            event.x,
            event.y,
            outline="#E0E0E0",
            width=2,
            dash=(5, 5),
        )
        self.selection_box = (self.start_x, self.start_y, event.x, event.y)

    def on_mouse_release(self, event):
        """Update final selection on mouse release."""
        if self.start_x is None or not self.dragging_selection:
            self.pending_start = None
            return
        self.selection_box = (self.start_x, self.start_y, event.x, event.y)
        self.pending_start = None
        self.dragging_selection = False
        log.debug("Selection updated, awaiting double-click confirmation")

    def on_double_click(self, event):
        """Confirm the current selection on double-click."""
        self.pending_start = None
        self.dragging_selection = False
        self.finish_selection()

    def finish_selection(self):
        """Crop selected area and copy it to the clipboard."""
        if self.selection_box is None:
            return

        # Get selection bounds (normalize to handle any drag direction)
        x1, y1, x2, y2 = self.selection_box
        left = min(x1, x2)
        top = min(y1, y2)
        right = max(x1, x2)
        bottom = max(y1, y2)

        # Ensure minimum size
        if right - left < 5 or bottom - top < 5:
            log.debug("Selection too small, ignoring")
            self.root.destroy()
            return

        # Crop and copy to clipboard
        cropped = self.screenshot.crop((left, top, right, bottom))
        log.debug(f"Selected area: left={left}, top={top}, right={right}, bottom={bottom}")
        copy_image_to_clipboard(cropped)

        # Show notification
        if _tray_icon:
            _tray_icon.notify("Screenshot copied to clipboard", "Screenshot")

        self.root.destroy()

    def on_escape(self, event):
        """Cancel screenshot on ESC key."""
        log.debug("Screenshot cancelled by user")
        self.root.destroy()

    def on_right_click(self, event):
        """Cancel screenshot on right-click."""
        log.debug("Screenshot cancelled by right-click")
        self.root.destroy()


# --- Low-level keyboard hook (no admin required) ---

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
VK_V = 0x56
VK_A = 0x41
HC_ACTION = 0

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Fix 64-bit types (ctypes defaults to c_int which truncates pointers on x64)
kernel32.GetModuleHandleW.restype = ctypes.wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [ctypes.wintypes.LPCWSTR]

user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int,           # idHook
    ctypes.c_void_p,        # lpfn (HOOKPROC)
    ctypes.wintypes.HINSTANCE,  # hMod
    ctypes.wintypes.DWORD,  # dwThreadId
]

user32.CallNextHookEx.restype = ctypes.c_long
user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p,        # hhk
    ctypes.c_int,           # nCode
    ctypes.wintypes.WPARAM, # wParam
    ctypes.wintypes.LPARAM, # lParam
]

user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

user32.PostMessageW.argtypes = [
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
]

HOOKPROC = ctypes.CFUNCTYPE(
    ctypes.wintypes.LPARAM,
    ctypes.c_int,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)

_hook_handle = None
_ctrl_pressed = False
_alt_pressed = False


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.wintypes.ULONG)),
    ]


def _low_level_keyboard_proc(nCode, wParam, lParam):
    """Low-level keyboard hook callback."""
    global _ctrl_pressed, _alt_pressed
    try:
        if nCode == HC_ACTION:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = kb.vkCode

            # Track Ctrl state
            if vk in (0xA2, 0xA3, 0x11):  # VK_LCONTROL, VK_RCONTROL, VK_CONTROL
                old_state = _ctrl_pressed
                _ctrl_pressed = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                if old_state != _ctrl_pressed:
                    log.debug(f"Ctrl state changed: {_ctrl_pressed}")

            # Track Alt state
            if vk in (0xA4, 0xA5, 0x12):  # VK_LMENU, VK_RMENU, VK_MENU
                old_state = _alt_pressed
                _alt_pressed = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                if old_state != _alt_pressed:
                    log.debug(f"Alt state changed: {_alt_pressed}")

            # Detect Ctrl+V (save clipboard image to file)
            if vk == VK_V and wParam == WM_KEYDOWN and _ctrl_pressed:
                log.info("Ctrl+V detected via hook!")
                threading.Thread(target=on_paste, daemon=True).start()

            # Detect Alt+A (Screenshot mode)
            # Note: When Alt is held, other keys generate WM_SYSKEYDOWN instead of WM_KEYDOWN
            if vk == VK_A and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN) and _alt_pressed:
                log.info("Alt+A detected via hook!")
                threading.Thread(target=on_screenshot, daemon=True).start()
    except Exception as e:
        log.error(f"Exception in hook callback: {e}", exc_info=True)

    return user32.CallNextHookEx(_hook_handle, nCode, wParam, lParam)


# Must keep a reference to prevent garbage collection
_hook_proc = HOOKPROC(_low_level_keyboard_proc)


def start_keyboard_hook():
    """Install the low-level keyboard hook and run message loop."""
    global _hook_handle
    try:
        log.info("Installing keyboard hook...")
        hmod = kernel32.GetModuleHandleW("user32.dll")
        log.info(f"Module handle: {hmod}")
        log.info(f"Hook proc: {_hook_proc}")
        _hook_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, ctypes.cast(_hook_proc, ctypes.c_void_p).value, hmod, 0
        )
        log.info(f"SetWindowsHookExW returned: {_hook_handle}")
        if not _hook_handle:
            err = ctypes.GetLastError()
            log.error(f"Failed to install hook, error: {err}")
            return

        log.info(f"Keyboard hook installed successfully (handle={_hook_handle})")

        # Message loop required for the hook to work
        msg = ctypes.wintypes.MSG()
        log.info("Entering message loop...")
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        log.info("Keyboard hook message loop ended")
    except Exception as e:
        log.error(f"Exception in start_keyboard_hook: {e}", exc_info=True)


def stop_keyboard_hook():
    """Remove the keyboard hook."""
    global _hook_handle
    if _hook_handle:
        user32.UnhookWindowsHookEx(_hook_handle)
        _hook_handle = None
        log.info("Keyboard hook removed")


def create_tray_icon():
    """Create and run the system tray icon."""
    global _tray_icon

    icon_path = os.path.join(get_script_dir(), "icon.ico")
    if os.path.exists(icon_path):
        icon_image = Image.open(icon_path)
    else:
        icon_image = Image.new("RGB", (64, 64), (70, 130, 180))

    def on_exit(icon, item):
        log.info("Exit requested")
        stop_keyboard_hook()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Clipboard Image Paster", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_exit),
    )

    icon = pystray.Icon("clipboard_image", icon_image, "Clipboard Image Paster", menu)
    _tray_icon = icon

    # Start keyboard hook in a separate thread (needs its own message loop)
    hook_thread = threading.Thread(target=start_keyboard_hook, daemon=True)
    hook_thread.start()

    def on_setup(icon):
        icon.visible = True
        icon.notify("Clipboard Image Paster is running", "Clipboard Image Paster")

    log.info("Starting tray icon...")
    icon.run(setup=on_setup)
    log.info("Tray icon stopped, exiting")


MUTEX_NAME = "ClipboardImagePaster_SingleInstance"
WM_CLOSE = 0x0010


def kill_previous_instance():
    """Find and close any previous instance of this program."""
    import win32process
    current_pid = os.getpid()

    def enum_callback(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid != current_pid and win32gui.GetWindowText(hwnd) == "ClipboardImagePaster_Hidden":
                log.info(f"Found previous instance (pid={pid}), sending WM_CLOSE")
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(enum_callback, None)
    import time
    time.sleep(0.5)


def _on_hidden_wnd_close(hwnd, msg, wp, lp):
    """Handle WM_CLOSE on hidden window - shut down everything."""
    log.info("WM_CLOSE received on hidden window, shutting down")
    stop_keyboard_hook()
    if _tray_icon:
        _tray_icon.stop()
    win32gui.DestroyWindow(hwnd)
    return 0


def create_hidden_window():
    """Create a hidden window for inter-process communication."""
    import win32api

    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = {WM_CLOSE: _on_hidden_wnd_close}
    wc.lpszClassName = "ClipboardImagePaster_WndClass"
    wc.hInstance = win32api.GetModuleHandle(None)
    try:
        cls = win32gui.RegisterClass(wc)
    except Exception:
        # Class already registered from a previous run in same process
        cls = win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindow(
        cls, "ClipboardImagePaster_Hidden",
        0, 0, 0, 0, 0, 0, 0, wc.hInstance, None
    )
    log.info(f"Hidden window created (hwnd={hwnd})")
    return hwnd


if __name__ == "__main__":
    log.info("=" * 40)
    log.info("Clipboard Image Paster starting")
    log.info(f"Log file: {LOG_PATH}")

    # Kill previous instance
    kill_previous_instance()

    try:
        _hidden_hwnd = create_hidden_window()
        create_tray_icon()
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
