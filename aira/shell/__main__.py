"""python -m aira.shell [--no-kiosk] [--port 8765]

Starts the shell server and shows it full-screen in Chromium (kiosk mode). Autostarted in the Xfce
session by /etc/xdg/autostart/aira-shell.desktop. The push-to-talk shortcut runs `aira-ptt`, which
posts to /ptt/toggle with the token from ~/.local/state/aira/shell.token.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..config import Config
from .server import Shell


def token_file() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "aira" / "shell.token"


def main(argv: list[str] | None = None) -> int:
    from ..orchestrator import Orchestrator
    from ..profiler.probe import probe
    from ..tts import choose_speaker
    from ..voice import Recorder, Transcriber, VoiceSession
    ap = argparse.ArgumentParser(prog="aira.shell")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-kiosk", action="store_true", help="do not open Chromium")
    args = ap.parse_args(argv)
    cfg = Config()
    shell = Shell(args.port, token_file=token_file())
    orch = Orchestrator(cfg, shell.confirm, start_controller=True)
    speaker = choose_speaker(probe().total_memory, out_dir=cfg.data_dir / "speech")
    shell.attach(orch, VoiceSession(orch, Transcriber(), speaker), Recorder(cfg.data_dir / "recordings"))
    shell.serve()
    url = f"http://127.0.0.1:{shell.port}/"
    print(f"AIRA shell on {url}", flush=True)
    kiosk = None
    if not args.no_kiosk:
        exe = shutil.which("chromium") or "/usr/bin/chromium"
        # full screen on an installed system; a normal window in the live session, so the desktop
        # and its "Install AIRUX" launcher stay reachable
        mode = [] if Path("/run/live/medium").exists() else ["--kiosk"]
        kiosk = subprocess.Popen([exe, *mode, f"--app={url}", "--no-first-run", "--noerrdialogs",
                                  "--password-store=basic",   # never ask the user to unlock a keyring
                                  f"--user-data-dir={cfg.data_dir / 'shell-profile'}"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    try:
        while not stop and (kiosk is None or kiosk.poll() is None):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    shell.stop()
    orch.browser.close()
    if kiosk and kiosk.poll() is None:
        kiosk.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
