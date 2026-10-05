"""Native window for the dashboard (macOS WKWebView via pywebview).

Starts the assetsboard web server in-process on 127.0.0.1 (first free port from 47651), opens a window
titled "AssetsBoard", and shuts the server down when the window closes (or on SIGTERM / Ctrl+C).
pywebview is the only non-stdlib dependency and lives in ~/assetsboard/.venv (see tools/build_mac_app.sh).
"""
from __future__ import annotations

import signal
import sys
from pathlib import Path

from . import web

APP_BUNDLE = Path.home() / "Applications" / "AssetsBoard.app"


def _brand_macos() -> None:
    """Show 'AssetsBoard' and its icon in the menu bar / Dock instead of 'Python' (best effort)."""
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle
        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = "AssetsBoard"
        info["CFBundleDisplayName"] = "AssetsBoard"
        icns = APP_BUNDLE / "Contents" / "Resources" / "AssetsBoard.icns"
        if icns.exists():
            img = NSImage.alloc().initWithContentsOfFile_(str(icns))
            if img is not None:
                NSApplication.sharedApplication().setApplicationIconImage_(img)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


_tick_refs: list = []


def _python_tick() -> None:
    """Wake the interpreter twice a second inside the Cocoa run loop so SIGTERM/Ctrl+C handlers run."""
    try:
        from Foundation import NSObject, NSTimer

        class _BGTick(NSObject):
            def tick_(self, _t):
                pass

        t = _BGTick.alloc().init()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.5, t, "tick:", None, True)
        _tick_refs.append(t)
    except Exception:  # noqa: BLE001
        pass


def main(project: Path, port: int = web.DEFAULT_PORT) -> int:
    try:
        import webview  # pywebview
    except ImportError:
        print("缺少 pywebview：請執行 tools/build_mac_app.sh（會建立 .venv 並安裝），或改用 python3 -m assetsboard web", file=sys.stderr)
        return 2
    httpd, url = web.start(project, port)
    print(f"AssetsBoard：{url}", flush=True)
    if sys.platform == "darwin":
        _brand_macos()
    try:
        win = webview.create_window("AssetsBoard", url, width=1400, height=900, min_size=(900, 600))

        def _close(*_):
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass
        signal.signal(signal.SIGTERM, _close)
        signal.signal(signal.SIGINT, _close)
        if sys.platform == "darwin":
            _python_tick()
        webview.start()  # blocks until the window is closed
    finally:
        httpd.shutdown()
        httpd.server_close()
        print("AssetsBoard：視窗已關閉，伺服器已停止", flush=True)
    return 0
