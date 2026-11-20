"""phagos-agent: Phagos in the user's session (autostarted at login).

  notifications  threats, suspicious files and paused programs, with buttons (Kill / Quarantine /
                 Allow) that send the user's choice back to phagosd
  the app        a local web server (127.0.0.1, random port, secret token) for the Phagos Security
                 window; it relays requests to phagosd as this user, so phagosd judges them by this
                 user's uid, and asks an administrator through polkit for admin actions
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .client import Client, PhagosError

UI = Path(__file__).with_name("ui")
METHODS = {"status", "scan", "scan_status", "history", "quarantine_list", "quarantine_restore", "quarantine_delete",
           "quarantine_file", "allowed_list", "allow", "unallow", "realtime", "decide"}


def state_dir() -> Path:
    d = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "phagos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def notify(title: str, body: str, actions: dict[str, str], urgency: str = "normal") -> str | None:
    """A desktop notification; returns the action the user clicked (None if dismissed or no daemon)."""
    if not shutil.which("notify-send"):
        return None
    cmd = ["notify-send", "-a", "Phagos", "-i", "phagos", "-u", urgency]
    if actions:
        cmd.append("--wait")
        for k, label in actions.items():
            cmd += ["-A", f"{k}={label}"]
    try:
        out = subprocess.run(cmd + [title, body], capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def open_app() -> None:
    subprocess.Popen(["phagos-security"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def on_event(ev: dict, client: Client) -> None:
    kind, threat, path = ev.get("kind"), ev.get("threat") or "", ev.get("path") or ""
    detail = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
    reasons = "; ".join((detail or {}).get("reasons", [])[:2])
    try:
        if kind == "threat":
            choice = notify("Threat removed", f"{threat} in {path}\n{ev.get('action', '')}.", {"open": "Open Phagos"},
                            "critical")
            if choice == "open":
                open_app()
        elif kind == "suspicious":
            choice = notify("Suspicious file", f"{threat}: {path}\n{reasons}",
                            {"quarantine": "Quarantine", "allow": "Allow", "open": "Details"})
            if choice == "quarantine":
                client.call("quarantine_file", timeout=300, path=path)
            elif choice == "allow" and (detail or {}).get("sha256"):
                client.call("allow", timeout=300, sha256=detail["sha256"], path=path, threat=threat)
            elif choice == "open":
                open_app()
        elif kind == "behavior":
            choice = notify("Phagos paused a program",
                            f"{path} (pid {ev.get('pid')}) acted dangerously: {reasons}.\nIt is paused until you choose.",
                            {"kill": "Stop it", "quarantine": "Stop and quarantine", "allow": "Allow"}, "critical")
            if choice in ("kill", "quarantine", "allow"):
                client.call("decide", event_id=ev["id"], choice=choice)
            elif choice is None:
                open_app()          # dismissed: the program stays paused; show where to decide
    except PhagosError as exc:
        notify("Phagos", f"That did not work: {exc}", {})


def watch(client: Client) -> None:
    while True:
        try:
            for ev in client.events():
                if ev.get("kind") in ("threat", "suspicious", "behavior"):
                    threading.Thread(target=on_event, args=(ev, client), daemon=True).start()
        except PhagosError:
            pass
        time.sleep(5)                # phagosd restarted or not up yet


def serve(client: Client) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(24)

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
            if self.path.split("?")[0] == "/":
                return self._send(200, (UI / "index.html").read_text().replace("__TOKEN__", token).encode(),
                                  "text/html; charset=utf-8")
            if self.path == "/phagos.svg":
                return self._send(200, (UI / "phagos.svg").read_bytes(), "image/svg+xml")
            self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            if not self._host_ok() or not secrets.compare_digest(self.headers.get("X-Phagos-Token", ""), token):
                return self._send(403, b'{"error":"forbidden"}')
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._send(400, b'{"error":"bad json"}')
            method = req.get("method")
            if method not in METHODS:
                return self._send(400, b'{"error":"unknown method"}')
            try:
                out = {"ok": True, **client.call(method, timeout=300, **(req.get("params") or {}))}
            except PhagosError as exc:
                out = {"ok": False, "error": str(exc)}
            self._send(200, json.dumps(out, default=str).encode())

        def do_OPTIONS(self):
            self._send(403, b'{"error":"no cross-origin requests"}')

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    f = state_dir() / "ui.token"
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{srv.server_port} {token}\n")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main(argv: list[str] | None = None) -> int:
    client = Client()
    srv = serve(client)
    print(f"phagos-agent: app on http://127.0.0.1:{srv.server_port}/", flush=True)
    try:
        watch(client)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
