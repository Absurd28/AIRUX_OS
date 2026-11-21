"""The Task Manager window's local server (127.0.0.1, random port, secret token; like Phagos Security).

GET  /                 the page (token embedded)
GET  /events?token=    server-sent events: confirm, confirm_done, result
GET  /icon?name=       an app icon from the icon theme
POST /api              {"method": ..., "params": {...}} with the X-Token header
    processes | performance | services | startup | users       read
    run {tool, args}                                            an action (through the broker)
    confirm {id, yes}                                           answer a confirmation
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import icons
from .actions import Confirmations, TaskManager

UI = Path(__file__).with_name("ui")
IDLE_EXIT = 600          # seconds without the window polling


def state_dir() -> Path:
    d = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "airux-taskmgr"
    d.mkdir(parents=True, exist_ok=True)
    return d


def action_sink():
    """Log to AIRA's encrypted vault (its action log) when it can be opened; else a private JSON-lines
    file in the Task Manager's state folder (mode 0600). Returns (sink, log_path)."""
    try:
        from aira.config import Config
        from aira.memory.keys import get_key
        from aira.memory.vault import Vault
        key, _ = get_key()
        vault = Vault(Config().vault_path, key)
        return (lambda e: vault.add("action", json.dumps({"via": "Task Manager", **e}, default=str), embed=False)), None
    except Exception:
        p = state_dir() / "actions.jsonl"
        if not p.exists():
            os.close(os.open(p, os.O_WRONLY | os.O_CREAT, 0o600))
        return None, p


class Hub:
    def __init__(self):
        self.subs: list[queue.Queue] = []
        self.lock = threading.Lock()

    def publish(self, ev: dict) -> None:
        with self.lock:
            for q in self.subs:
                q.put(ev)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)


def serve(tm: TaskManager | None = None, port: int = 0, token_file: Path | None = None):
    hub = Hub()
    conf = Confirmations(hub.publish)
    if tm is None:
        sink, log_path = action_sink()
        tm = TaskManager(conf.ask, sink=sink, log_path=log_path).start()
    token = secrets.token_urlsafe(24)
    last = [time.time()]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            return self.headers.get("Host") in (f"127.0.0.1:{srv.server_port}", f"localhost:{srv.server_port}")

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b'{"error":"bad host"}')
            path, _, q = self.path.partition("?")
            if path == "/":
                return self._send(200, (UI / "index.html").read_text().replace("__TOKEN__", token).encode(),
                                  "text/html; charset=utf-8")
            if path == "/logo.svg":
                return self._send(200, (UI / "airux-taskmgr.svg").read_bytes(), "image/svg+xml")
            if path == "/icon":
                f = icons.find(q.partition("name=")[2].split("&")[0])
                if f is None:
                    return self._send(404, b"{}")
                return self._send(200, f.read_bytes(), icons.CTYPES[f.suffix])
            if path == "/events":
                if not secrets.compare_digest(q.partition("token=")[2], token):
                    return self._send(403, b'{"error":"token"}')
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                sub = hub.subscribe()
                try:
                    for ev in conf.events():
                        self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            ev = sub.get(timeout=15)
                            self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    hub.unsubscribe(sub)
                return
            self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            if not self._host_ok() or not secrets.compare_digest(self.headers.get("X-Token", ""), token):
                return self._send(403, b'{"error":"forbidden"}')
            last[0] = time.time()
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._send(400, b'{"error":"bad json"}')
            m, a = req.get("method"), req.get("params") or {}
            if m == "processes":
                out = tm.processes()
            elif m == "performance":
                out = tm.performance()
            elif m == "services":
                out = tm.services()
            elif m == "startup":
                out = tm.startup_apps()
            elif m == "users":
                out = tm.users()
            elif m == "confirm":
                out = {"ok": conf.answer(str(a.get("id", "")), bool(a.get("yes")))}
            elif m == "run":
                rid = secrets.token_hex(6)
                tool, args = str(a.get("tool", "")), a.get("args") or {}

                def work():
                    hub.publish({"event": "result", "rid": rid, "tool": tool, **tm.run(tool, args)})
                threading.Thread(target=work, daemon=True).start()
                out = {"ok": True, "rid": rid}
            else:
                return self._send(400, b'{"error":"unknown method"}')
            self._send(200, json.dumps(out, default=str).encode())

        def do_OPTIONS(self):
            self._send(403, b'{"error":"no cross-origin requests"}')

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    f = token_file or state_dir() / "ui.token"
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{srv.server_port} {token}\n")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.tm, srv.token, srv.last_used, srv.confirmations = tm, token, last, conf
    return srv


def main() -> int:
    srv = serve()
    print(f"AIRUX Task Manager on http://127.0.0.1:{srv.server_port}/", flush=True)
    while time.time() - srv.last_used[0] < IDLE_EXIT:
        time.sleep(15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
