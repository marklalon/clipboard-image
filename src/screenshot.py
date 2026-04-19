"""
Little Helper - Fullscreen screenshot selector.
"""

import logging
import queue
import threading
import tkinter as tk
from PIL import Image, ImageGrab, ImageTk

from clipboard_paste import copy_image_to_clipboard

log = logging.getLogger("little_helper.screenshot")

_ui_root = None
_ui_thread_id = None
_ui_tasks: queue.Queue = queue.Queue()
_selector_instance = None


def set_ui_root(root) -> None:
    """Register the shared Tk UI root used by screenshot selection."""
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


class ScreenshotSelector:
    """Fullscreen screenshot selector with area selection."""

    def __init__(self, notify_fn=None):
        self.notify_fn = notify_fn
        self.root = None
        self.canvas = None
        self.screenshot = None
        self.photo = None
        self.start_x = None
        self.start_y = None
        self.rect_id = None
        self.rect_outer_id = None
        self.selection_box = None
        self.pending_start = None
        self.dragging_selection = False

    def show(self, parent):
        """Show the screenshot selector on the shared Tk UI thread."""
        self.screenshot = ImageGrab.grab()
        screen_width, screen_height = self.screenshot.size

        self.root = tk.Toplevel(parent)
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.config(bg="black")
        self.root.bind("<Destroy>", self.on_destroy, add="+")

        self.canvas = tk.Canvas(
            self.root,
            width=screen_width,
            height=screen_height,
            bg="black",
            highlightthickness=0,
        )
        self.canvas.pack()

        self.photo = ImageTk.PhotoImage(self.screenshot, master=self.root)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        border_width = 4
        border_color = "#FF8C00"
        for x1, y1, x2, y2 in [
            (0, 0, screen_width, border_width),
            (0, screen_height - border_width, screen_width, screen_height),
            (0, 0, border_width, screen_height),
            (screen_width - border_width, 0, screen_width, screen_height),
        ]:
            self.canvas.create_rectangle(x1, y1, x2, y2, fill=border_color, outline="")

        self.canvas.bind("<ButtonPress-1>", self.on_mouse_press)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<Double-Button-1>", self.on_double_click)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_release)
        self.canvas.bind("<Button-3>", self.on_right_click)
        self.root.bind("<Escape>", self.on_escape)
        self.root.focus_force()

    def on_destroy(self, event):
        global _selector_instance
        if event.widget is self.root and _selector_instance is self:
            _selector_instance = None

    def on_mouse_press(self, event):
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
        if self.pending_start is not None and not self.dragging_selection:
            self.start_x, self.start_y = self.pending_start
            self.pending_start = None
            self.dragging_selection = True
            self.selection_box = (self.start_x, self.start_y, event.x, event.y)

        if self.start_x is None:
            return

        if self.rect_outer_id:
            self.canvas.delete(self.rect_outer_id)
        if self.rect_id:
            self.canvas.delete(self.rect_id)

        self.rect_outer_id = self.canvas.create_rectangle(
            self.start_x - 1, self.start_y - 1, event.x + 1, event.y + 1,
            outline="white", width=3,
        )
        self.rect_id = self.canvas.create_rectangle(
            self.start_x, self.start_y, event.x, event.y,
            outline="#111111", width=1, dash=(6, 4),
        )
        self.selection_box = (self.start_x, self.start_y, event.x, event.y)

    def on_mouse_release(self, event):
        if self.start_x is None or not self.dragging_selection:
            self.pending_start = None
            return
        self.selection_box = (self.start_x, self.start_y, event.x, event.y)
        self.pending_start = None
        self.dragging_selection = False
        log.debug("Selection updated, awaiting double-click confirmation")

    def on_double_click(self, event):
        self.pending_start = None
        self.dragging_selection = False
        self.finish_selection()

    def finish_selection(self):
        if self.selection_box is None:
            return
        x1, y1, x2, y2 = self.selection_box
        left, right = min(x1, x2), max(x1, x2)
        top, bottom = min(y1, y2), max(y1, y2)

        if right - left < 5 or bottom - top < 5:
            log.debug("Selection too small, ignoring")
            self.root.destroy()
            return

        cropped = self.screenshot.crop((left, top, right, bottom))
        log.debug(f"Selected area: left={left}, top={top}, right={right}, bottom={bottom}")
        copy_image_to_clipboard(cropped)

        if self.notify_fn:
            self.notify_fn("Screenshot copied to clipboard", "Screenshot")

        self.root.destroy()

    def on_escape(self, event):
        log.debug("Screenshot cancelled by user")
        self.root.destroy()

    def on_right_click(self, event):
        log.debug("Screenshot cancelled by right-click")
        self.root.destroy()


def on_screenshot(config: dict, notify_fn=None) -> None:
    """Handle screenshot hotkey: launch screenshot selection mode."""
    log.debug("on_screenshot triggered")
    def _show_impl():
        global _selector_instance

        try:
            if _selector_instance is not None:
                try:
                    if _selector_instance.root is not None and _selector_instance.root.winfo_exists():
                        _selector_instance.root.lift()
                        _selector_instance.root.focus_force()
                        return
                except Exception:
                    _selector_instance = None

            selector = ScreenshotSelector(notify_fn=notify_fn)
            _selector_instance = selector
            selector.show(_ui_root)
        except Exception as e:
            log.error(f"Error in on_screenshot: {e}", exc_info=True)
            _selector_instance = None

    try:
        _run_on_ui_thread(_show_impl)
    except Exception as e:
        log.error(f"Error scheduling screenshot UI: {e}", exc_info=True)
