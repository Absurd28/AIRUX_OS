"""Talk to phagosd over its socket (JSON lines). Used by the CLI, the agent and the app."""
from __future__ import annotations

import json
import os
import socket
from typing import Iterator

SOCKET = os.environ.get("PHAGOS_SOCKET", "/run/phagos/phagos.sock")


class PhagosError(Exception):
    pass


class Client:
    def __init__(self, path: str = SOCKET, timeout: float = 30):
        self.path, self.timeout = path, timeout

    def _connect(self, timeout: float | None) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(self.path)
        except OSError as exc:
            s.close()
            raise PhagosError("Phagos is not running (phagosd)") from exc
        return s

    def call(self, method: str, timeout: float | None = None, **params) -> dict:
        s = self._connect(timeout or self.timeout)
        try:
            s.sendall((json.dumps({"method": method, "params": params}) + "\n").encode())
            line = s.makefile("rb").readline()
        except OSError as exc:
            raise PhagosError(f"no answer from Phagos: {exc}") from exc
        finally:
            s.close()
        if not line:
            raise PhagosError("Phagos closed the connection")
        out = json.loads(line)
        if not out.pop("ok", False):
            raise PhagosError(out.get("error", "unknown error"))
        return out

    def events(self) -> Iterator[dict]:
        """Live events for this user (threats, paused processes, decisions); blocks."""
        s = self._connect(None)
        s.sendall(b'{"method": "subscribe"}\n')
        f = s.makefile("rb")
        f.readline()
        for line in f:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "event" in msg:
                yield msg["event"]
