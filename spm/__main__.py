"""Start Skill Pack Manager: local server + app window.

  python -m spm             open the window (Edge/Chrome app mode, else the default browser)
  python -m spm --no-window print the URL only (headless, or to use your own browser)

The token travels in the URL fragment, which the browser never sends to the
server. The process exits 30 s after the window stops sending heartbeats.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import traceback
import webbrowser

from .osinfo import app_browser, data_dir
from .server import App, serve


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="spm", description=__doc__.splitlines()[0])
    ap.add_argument("--no-window", action="store_true", help="print the URL and keep serving")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args(argv)

    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.executable.lower().endswith("pythonw.exe"):
        log = open(d / "spm.log", "a", encoding="utf8", buffering=1)  # noqa: SIM115 - lives as long as the process
        sys.stdout = sys.stderr = log
    print(f"--- start {time.strftime('%Y-%m-%d %H:%M:%S')}")

    app = App()
    srv, port = serve(app, a.port)
    url = f"http://127.0.0.1:{port}/#t={app.token}"

    if a.no_window:
        print(url, flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    browser = app_browser()
    if browser:
        subprocess.Popen([*browser, f"--app={url}", "--window-size=1320,880",
                          f"--user-data-dir={d / 'browser-profile'}", "--no-first-run",
                          "--no-default-browser-check"])
    else:
        webbrowser.open(url)

    app.last_ping = time.time() + 30  # grace period while the window starts
    while time.time() - app.last_ping < 30:
        time.sleep(2)
    print("no heartbeat, exiting")
    srv.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
