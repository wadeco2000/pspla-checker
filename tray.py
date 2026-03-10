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
    """Police siren icon — red/blue when running, grey when stopped."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if running:
        red  = (220, 40,  40)
        blue = (40,  80, 220)
        housing = (40, 40, 50)
        shine_r = (255, 160, 160, 140)
        shine_b = (160, 160, 255, 140)
    else:
        red  = (110, 90,  90)
        blue = (90,  90, 110)
        housing = (80, 80, 80)
        shine_r = shine_b = None

    # Siren housing base
    draw.rectangle([8, 38, 56, 52], fill=housing)
    draw.rectangle([12, 50, 52, 58], fill=housing)

    # Left dome (red)
    draw.ellipse([6, 14, 34, 42], fill=red)
    # Right dome (blue)
    draw.ellipse([30, 14, 58, 42], fill=blue)
    # Centre divider so domes look separate
    draw.rectangle([29, 14, 35, 42], fill=housing)

    # Shine glints on each dome when running
    if shine_r:
        draw.ellipse([10, 18, 22, 27], fill=shine_r)
        draw.ellipse([42, 18, 54, 27], fill=shine_b)

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


def restart(icon, item):
    stop(icon, None)
    start(icon, None)


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
        pystray.MenuItem("Restart", restart),
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
