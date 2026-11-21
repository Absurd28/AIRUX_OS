"""The link to the AIRA shell (127.0.0.1, token in ~/.local/state/aira/shell.token), like NEOX's
native host: phone commands go to /command, spoken ones to /voice_file, answers to /confirm, and the
shell's events (transcript, agents, confirmations) are relayed to the phones."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


def token_file() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "aira" / "shell.token"


class AiraError(Exception):
    pass


class AiraLink:
    def __init__(self, on_event: Callable[[dict], None], tfile: Path | None = None):
        self.on_event = on_event
        self.tfile = tfile or token_file()
        self.connected = False
        self._stop = False
        self._thread: threading.Thread | None = None

    def _creds(self) -> tuple[str, str]:
        try:
            port, token = self.tfile.read_text().split()
        except (OSError, ValueError) as exc:
            raise AiraError("AIRA isn't running on the PC") from exc
        return f"http://127.0.0.1:{port}", token

    def post(self, path: str, body: dict, timeout: float = 15) -> dict:
        base, token = self._creds()
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-AIRA-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise AiraError("AIRA is busy with another request") from exc
            raise AiraError(f"AIRA refused ({exc.code})") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise AiraError("AIRA isn't answering") from exc

    def _retry_busy(self, path: str, body: dict, tries: int = 8) -> dict:
        """The shell says 'idle' a moment before it is free again: a command right after a turn retries."""
        for i in range(tries):
            try:
                return self.post(path, body)
            except AiraError as exc:
                if "busy" not in str(exc) or i == tries - 1:
                    raise
                time.sleep(0.25)
        return {}

    def command(self, text: str, via: str = "phone") -> None:
        self._retry_busy("/command", {"text": text, "via": via})

    def voice(self, wav: Path, quiet_pc: bool = True) -> None:
        self._retry_busy("/voice_file", {"path": str(wav), "quiet": quiet_pc, "via": "phone"})

    def confirm(self, cid: str, yes: bool) -> bool:
        return bool(self.post("/confirm", {"id": cid, "yes": yes}).get("ok"))

    def settings(self) -> dict:
        return self.post("/settings", {})

    # ---- events
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="connect-aira-events", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def _loop(self) -> None:
        while not self._stop:
            try:
                base, token = self._creds()
                with urllib.request.urlopen(f"{base}/events?token={token}", timeout=60) as r:
                    self.connected = True
                    self.on_event({"event": "link", "up": True})
                    for raw in r:
                        if self._stop:
                            return
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("data:"):
                            try:
                                self.on_event(json.loads(line[5:]))
                            except ValueError:
                                pass
            except (AiraError, urllib.error.URLError, OSError, TimeoutError):
                pass
            if self.connected:
                self.connected = False
                self.on_event({"event": "link", "up": False})
            time.sleep(3)
