"""
Little Helper - Clipboard image paste and Explorer path detection.
Provides functions to detect clipboard images and paste them to Explorer windows.
"""

import os
import logging
import urllib.parse
from datetime import datetime
from io import BytesIO

import ctypes
import ctypes.wintypes

import pythoncom
import win32gui
import win32com.client
import win32clipboard
import win32con
import win32process
from PIL import Image, ImageGrab

log = logging.getLogger("little_helper.clipboard_paste")

# Window class names that indicate editable input fields (non-Explorer)
EDITABLE_WINDOW_CLASSES = {
    "Edit",
    "RichEdit20W",
    "RichEdit50W",
    "DirectUIHost",
    "Chrome_RenderWidgetHostHWND",
    "Chrome_WidgetWin_1",
    "MozillaWindowClass",
    "Internet Explorer_Server",
}

# Explorer address bar / search box focused-child classes
EXPLORER_EDITABLE_CLASSES = {
    "Edit",
    "ComboBox",
    "SearchEditBoxWrapperClass",
    "ToolbarWindow32",
    "Address Band Root",
}


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("hwndActive", ctypes.wintypes.HWND),
        ("hwndFocus", ctypes.wintypes.HWND),
        ("hwndCapture", ctypes.wintypes.HWND),
        ("hwndMenuOwner", ctypes.wintypes.HWND),
        ("hwndMoveSize", ctypes.wintypes.HWND),
        ("hwndCaret", ctypes.wintypes.HWND),
        ("rcCaret", ctypes.wintypes.RECT),
    ]


def _get_focused_child(hwnd: int) -> int:
    """
    Get the focused child window of *hwnd* via GetGUIThreadInfo.
    Works across threads (unlike GetFocus).  Returns 0 on failure.
    """
    try:
        tid, _ = win32process.GetWindowThreadProcessId(hwnd)
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if ctypes.windll.user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
            fh = info.hwndFocus
            if fh and fh != hwnd:
                return fh
    except Exception as e:
        log.debug(f"_get_focused_child error: {e}")
    return 0


def should_skip_paste() -> bool:
    """
    Return True when the user is typing in an input field and the
    normal Ctrl+V should be left alone (address bar, search box, browser, etc.).
    """
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return False

    cls = win32gui.GetClassName(hwnd)

    # --- Explorer window --------------------------------------------------
    if cls in ("CabinetWClass", "ExploreWClass"):
        focus = _get_focused_child(hwnd)
        if focus:
            fcls = win32gui.GetClassName(focus)
            log.debug(f"Explorer focused child: hwnd={focus}, class={fcls}")
            if fcls in EXPLORER_EDITABLE_CLASSES:
                log.debug("Explorer editable control focused – skip paste")
                return True
            # Modern address bar uses DirectUIHWND inside an "Address" band
            if fcls == "DirectUIHWND":
                parent = win32gui.GetParent(focus)
                while parent and parent != hwnd:
                    pcls = win32gui.GetClassName(parent)
                    if "Address" in pcls or "Search" in pcls or "Breadcrumb" in pcls:
                        log.debug("Modern address bar focused – skip paste")
                        return True
                    parent = win32gui.GetParent(parent)
        log.debug("Explorer file-list focused – allow paste")
        return False

    # --- Non-Explorer: check if it's an editable window class -------------
    if cls in EDITABLE_WINDOW_CLASSES:
        return True

    return False


def get_explorer_path() -> str | None:
    """Get the directory path of the currently focused Explorer window."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        log.debug("No foreground window")
        return None

    class_name = win32gui.GetClassName(hwnd)
    log.debug(f"Foreground window class: {class_name}, hwnd: {hwnd}")

    # Desktop window -> use Desktop path
    if class_name in ("WorkerW", "Progman"):
        desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
        if os.path.isdir(desktop):
            log.debug(f"Desktop detected, path: {desktop}")
            return desktop
        log.debug("Desktop detected but path not found")
        return None

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


def generate_filename(directory: str) -> str:
    """Generate a unique clipboard-*.png filename."""
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


def has_clipboard_file_paths() -> bool:
    """Check if clipboard contains file paths (not image data)."""
    try:
        win32clipboard.OpenClipboard()
        try:
            # CF_HDROP (15) is the standard format for file lists
            # If this format exists, it means files are in clipboard
            return win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP)
        finally:
            win32clipboard.CloseClipboard()
    except Exception as e:
        log.debug(f"Error checking clipboard formats: {e}")
        return False


def get_clipboard_image() -> Image.Image | None:
    """Return PIL Image from clipboard, or None."""
    img = ImageGrab.grabclipboard()
    if isinstance(img, Image.Image):
        log.debug("Got image via ImageGrab.grabclipboard()")
        return img
    if isinstance(img, list):
        for path in img:
            try:
                return Image.open(path)
            except Exception:
                pass
    return None


def copy_image_to_clipboard(img: Image.Image) -> None:
    """Copy a PIL Image to the Windows clipboard as CF_DIB."""
    if img.mode != "RGB":
        img = img.convert("RGB")

    output = BytesIO()
    try:
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


def on_paste(config: dict, notify_fn=None) -> None:
    """Handle paste hotkey: save clipboard image to active Explorer directory."""
    log.debug("on_paste triggered")
    
    # Check if clipboard contains file paths (not image data)
    # If yes, Windows will handle it, so skip to avoid duplicate files
    if has_clipboard_file_paths():
        log.debug("Clipboard contains file paths - letting Windows handle paste")
        return
    
    # Check if we should skip paste in editable contexts
    skip_result = should_skip_paste()
    log.debug(f"DEBUG: should_skip_paste() returned {skip_result}")
    if skip_result:
        log.debug("Skipping paste in editable context - allowing normal Ctrl+V")
        return
    
    try:
        img = get_clipboard_image()
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
