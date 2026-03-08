import os
import sys
import subprocess
import webbrowser
import threading
from PIL import Image, ImageDraw
import pystray

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_SCRIPT = os.path.join(BASE_DIR, "dashboard.py")
DASHBOARD_URL = "http://localhost:5000"

_process = None


def make_icon(running):
    """Green circle = running, grey circle = stopped."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Outer circle
    color = (39, 174, 96) if running else (120, 120, 120)
    draw.ellipse([2, 2, 62, 62], fill=color)
    # Inner white ring (camera lens look)
    draw.ellipse([16, 16, 48, 48], fill=(255, 255, 255, 200))
    draw.ellipse([24, 24, 40, 40], fill=color)
    return img


def is_running():
    return _process is not None and _process.poll() is None


def start(icon, item):
    global _process
    if is_running():
        return
    _process = subprocess.Popen(
        [sys.executable, DASHBOARD_SCRIPT],
        cwd=BASE_DIR,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    icon.icon = make_icon(True)
    icon.title = "PSPLA Checker — Running"
    # Give Flask a moment to start before opening the browser
    threading.Timer(1.5, lambda: webbrowser.open(DASHBOARD_URL)).start()


def stop(icon, item):
    global _process
    if _process:
        _process.terminate()
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _process.kill()
    _process = None
    icon.icon = make_icon(False)
    icon.title = "PSPLA Checker — Stopped"


def open_browser(icon, item):
    webbrowser.open(DASHBOARD_URL)


def quit_app(icon, item):
    stop(icon, None)
    icon.stop()


def main():
    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", open_browser, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start", start),
        pystray.MenuItem("Stop", stop),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon(
        "pspla_checker",
        make_icon(False),
        "PSPLA Checker — Stopped",
        menu,
    )
    icon.run()


if __name__ == "__main__":
    main()
