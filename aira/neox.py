"""AIRA browses in NEOX, the AIRUX browser.

    AIRA's agents --(NeoxBridge, Unix socket)--> native host (aira.neox_host) <--(native messaging)--> NEOX extension

The NEOX extension (neox/extension) runs each command in the user's real NEOX window and returns what
the page now shows. No debugging port is opened: the socket lives in $XDG_RUNTIME_DIR/aira (mode
0700), and Chromium only starts the native host for NEOX's own extension (its id is fixed by the key
in the extension's manifest).

NeoxBridge has the same interface as aira.browser.BrowserSession (open, search, read, click, back,
close), so the browser tool, its risks and its read-backs are unchanged.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from .browser import BrowserError

EXTENSION_ID = "caeflmcomdjlfjeinfhpookmojmhiphd"
HOST_NAME = "org.airux.neox"


def socket_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/aira-{os.getuid()}"
    return Path(base) / "aira" / "neox.sock"


# ---------------------------------------------------------------- native messaging framing
def read_native(stream) -> dict | None:
    """One Chrome native-messaging message: 4-byte native-endian length, then UTF-8 JSON."""
    head = stream.read(4)
    if len(head) < 4:
        return None
    (n,) = struct.unpack("=I", head)
    if n > 64 * 1024 * 1024:
        raise ValueError("native message too large")
    body = stream.read(n)
    if len(body) < n:
        return None
    return json.loads(body.decode("utf-8"))


def write_native(stream, msg: dict) -> None:
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    if len(body) > 1024 * 1024:     # Chrome's limit for host -> extension messages
        raise ValueError("message to the browser too large")
    stream.write(struct.pack("=I", len(body)) + body)
    stream.flush()


# ---------------------------------------------------------------- AIRA's side
class NeoxBridge:
    """AIRA's browser, backed by NEOX. Listens on the private socket; the native host connects."""

    def __init__(self, sock_path: Path | None = None, launch: list[str] | None = None, wait_s: float = 25,
                 timeout_s: float = 60):
        self.sock_path = sock_path or socket_path()
        self.launch = launch if launch is not None else ([shutil.which("neox")] if shutil.which("neox") else [])
        self.wait_s, self.timeout_s = wait_s, timeout_s
        self._conn: socket.socket | None = None
        self._lock = threading.Lock()
        self._results: dict[str, dict] = {}
        self._cv = threading.Condition()
        self._n = 0
        self._server: socket.socket | None = None
        self._start_server()

    def _start_server(self) -> None:
        d = self.sock_path.parent
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        if self.sock_path.exists():
            try:   # another AIRA process already serves NEOX: refuse rather than steal its socket
                probe = socket.socket(socket.AF_UNIX)
                probe.connect(str(self.sock_path))
                probe.close()
                raise BrowserError("another AIRA process is already connected to NEOX")
            except ConnectionRefusedError:
                self.sock_path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.sock_path))
        os.chmod(self.sock_path, 0o600)
        srv.listen(2)
        self._server = srv
        threading.Thread(target=self._accept_loop, name="aira-neox-bridge", daemon=True).start()

    def _accept_loop(self) -> None:
        while self._server is not None:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with self._cv:
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except OSError:
                        pass
                self._conn = conn
                self._cv.notify_all()
            threading.Thread(target=self._read_loop, args=(conn,), daemon=True).start()

    def _read_loop(self, conn: socket.socket) -> None:
        f = conn.makefile("rb")
        try:
            for line in f:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("type") == "result" and "id" in msg:
                    with self._cv:
                        self._results[msg["id"]] = msg
                        self._cv.notify_all()
        except OSError:
            pass   # the connection was closed (NEOX quit, or close())
        with self._cv:
            if self._conn is conn:
                self._conn = None
            self._cv.notify_all()

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def _ensure_connected(self) -> None:
        if self._conn is not None:
            return
        if not self.launch:
            raise BrowserError("NEOX is not installed")
        subprocess.Popen(self.launch, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        end = time.monotonic() + self.wait_s
        with self._cv:
            while self._conn is None and time.monotonic() < end:
                self._cv.wait(timeout=0.5)
        if self._conn is None:
            raise BrowserError(f"NEOX did not connect within {self.wait_s:.0f}s")

    def _call(self, cmd: str, **args) -> dict:
        with self._lock:
            self._ensure_connected()
            self._n += 1
            mid = f"a{self._n}"
            line = json.dumps({"type": "command", "id": mid, "cmd": cmd, **args}).encode() + b"\n"
            try:
                self._conn.sendall(line)
            except OSError as exc:
                raise BrowserError(f"lost the connection to NEOX ({exc})") from None
            end = time.monotonic() + self.timeout_s
            with self._cv:
                while mid not in self._results and time.monotonic() < end and self._conn is not None:
                    self._cv.wait(timeout=0.5)
                r = self._results.pop(mid, None)
        if r is None:
            raise BrowserError(f"NEOX did not answer within {self.timeout_s:.0f}s")
        if not r.get("ok"):
            raise BrowserError(str(r.get("error", "NEOX could not do that"))[:300])
        return r["value"]

    # the BrowserSession interface
    def open(self, url: str = "") -> dict:
        return self._call("open", url=url)

    def search(self, query: str) -> dict:
        return self._call("search", query=query)

    def read(self) -> dict:
        return self._call("read")

    def click(self, text: str) -> dict:
        return self._call("click", text=text)

    def back(self) -> dict:
        return self._call("back")

    def close(self) -> None:
        srv, self._server = self._server, None
        if srv is not None:
            srv.close()
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        try:
            self.sock_path.unlink()
        except OSError:
            pass
