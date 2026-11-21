"""Runs the desktop helper (portal_helper.py, Debian's python3) and the screen streams."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

PKG_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PYTHON = "/usr/bin/python3"


def helper_cmd(*args: str) -> list[str]:
    py = SYSTEM_PYTHON if os.path.exists(SYSTEM_PYTHON) else sys.executable
    return [py, "-s", "-m", "airox_connect.portal_helper", *args]


def helper_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PKG_ROOT)          # only this package: no AIRA venv modules
    env.pop("VIRTUAL_ENV", None)
    return env


class Desktop:
    """The long-running helper: remote input (one RemoteDesktop session) and the notification monitor."""

    def __init__(self, on_event: Callable[[dict], None]):
        self.on_event = on_event
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.input_state = "off"

    def available(self) -> bool:
        return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")) and \
            os.environ.get("DBUS_SESSION_BUS_ADDRESS") is not None

    def start(self) -> bool:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return True
            if not self.available():
                return False
            try:
                self.proc = subprocess.Popen(helper_cmd("daemon"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, text=True, bufsize=1, env=helper_env())
            except OSError:
                self.proc = None
                return False
        threading.Thread(target=self._read, args=(self.proc,), name="connect-desktop", daemon=True).start()
        return True

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("ev") == "input":
                self.input_state = ev.get("state", "off")
            self.on_event(ev)
        self.input_state = "off"
        self.on_event({"ev": "desktop_helper", "running": False})

    def send(self, msg: dict) -> bool:
        if not self.start():
            return False
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def stop(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()


class Stream:
    """One screen stream (second screen or mirror): the helper's stdout is H.264, its stderr status."""

    def __init__(self, source: str, fps: int, kbps: int, on_status: Callable[[dict], None], encoder: str = "auto"):
        self.source, self.fps, self.kbps, self.encoder = source, fps, kbps, encoder
        self.on_status = on_status
        self.proc: subprocess.Popen | None = None
        self.info: dict = {}

    def start(self) -> subprocess.Popen:
        if not shutil.which("gst-launch-1.0"):
            raise RuntimeError("GStreamer is not installed on the PC")
        self.proc = subprocess.Popen(helper_cmd("stream", "--source", self.source, "--fps", str(self.fps),
                                                "--kbps", str(self.kbps), "--encoder", self.encoder),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=helper_env(), bufsize=0)
        threading.Thread(target=self._status, name="connect-stream-status", daemon=True).start()
        return self.proc

    def _status(self) -> None:
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("@@"):
                try:
                    ev = json.loads(line[2:])
                except ValueError:
                    continue
                if ev.get("ev") == "stream":
                    self.info = ev
                self.on_status(ev)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
