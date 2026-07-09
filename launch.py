"""
On-demand launcher for Simple Budget -- double-clicked via a desktop icon
(see "Open Simple Budget.vbs"). Starts the local server only if it isn't
already running (no scheduled task, nothing running when you're not using
the app), then opens the app in its own standalone window.
"""

import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).parent
PYTHONW = ROOT.parent / "py314" / "pythonw.exe"
PORT = 5112


def server_running():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def app_browser_path():
    for p in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(p).exists():
            return p
    return None


def main():
    if not server_running():
        subprocess.Popen(
            [str(PYTHONW), str(ROOT / "server.py")],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(30):
            if server_running():
                break
            time.sleep(0.3)

    url = f"http://localhost:{PORT}"
    browser = app_browser_path()
    if browser:
        # --app opens a standalone window: no tabs, no address bar --
        # feels like a real app instead of a browser bookmark.
        subprocess.Popen([browser, f"--app={url}"])
    else:
        import webbrowser
        webbrowser.open(url)


if __name__ == "__main__":
    main()
