# Little Helper

A lightweight Windows system tray tool that automatically saves clipboard images as PNG files to the currently active Explorer folder when you press **Ctrl+V**.

## Features

- **System tray resident** - runs silently in the background with a tray icon
- **Built-in monitor server** - optionally expose hardware monitor data over HTTP and WebSocket
- **Auto-detect target folder** - identifies the currently focused Explorer window or Desktop and saves images there
- **Keyboard hook (no admin required)** - uses low-level Windows keyboard hook to capture Ctrl+V globally
- **Smart filename generation** - saves as `clipboard-YYYYMMDD-HHMMSS.png` with auto-increment for duplicates
- **Single instance** - automatically closes previous instances when relaunched
- **Non-intrusive** - only activates when the clipboard contains an image AND an Explorer window is focused; normal Ctrl+V behavior is unaffected elsewhere

## Requirements

- Windows 10/11
- Python 3.8+

## Installation

```bash
pip install -r requirements.txt
```

Dependencies: `pystray`, `Pillow`, `keyboard`, `pywin32`, `starlette`, `uvicorn`, `websockets`

## Usage

**Quick start:**

Double-click `run.bat`, or run from command line:

```bash
pythonw clipboard_image.pyw
```

**How to use:**

1. Launch the tool - a tray icon appears in the system tray
2. Copy an image to clipboard (e.g., screenshot with Win+Shift+S, or copy from browser)
3. Open or focus an Explorer window in the target folder
4. Press **Ctrl+V** - the image is saved as a PNG file in that folder

**Exit:** Right-click the tray icon and select "Exit".

## Logs

Debug logs are written to `clipboard_image.log` in the same directory as the script.

## Monitor Server

Enable **Monitor Server** in Settings to expose the same hardware stats used by the overlay.

- Default bind address: `0.0.0.0:9980`
- HTTP snapshot endpoint: `/api/monitor`
- WebSocket endpoint: `/ws/monitor`
- Optional auth: set a token in Settings, then pass it as `Authorization: Bearer <token>`, `X-Monitor-Token: <token>`, or `?token=<token>`
