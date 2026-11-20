"""AIRUX Drivers for the user: status, the hardware report, running a fix through the root helper,
and the app's local web server (127.0.0.1, random port, secret token; like Phagos Security)."""
from __future__ import annotations

import datetime as dt
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

from . import __version__, inventory
from .diagnose import CATEGORIES, FirmwareIndex, diagnose, summary

UI = Path(__file__).with_name("ui")
HELPER = "/usr/lib/airux-drivers/airux-drivers-helper"


def status(root: str = "/") -> dict:
    inv = inventory.collect(root)
    findings = diagnose(inv, FirmwareIndex(root))
    return {"version": __version__, "summary": summary(findings), "findings": [f.to_dict() for f in findings],
            "secure_boot": inv.secure_boot, "live": inv.live,
            "firmware_failures": inv.firmware_failures,
            "counts": {"pci": len(inv.pci), "usb": len(inv.usb)},
            "devices": [{"bus": d.bus, "address": d.address, "name": d.name, "driver": d.driver,
                         "id": f"{d.vendor}:{d.device}"} for d in inv.pci + inv.usb]}


def report(st: dict | None = None) -> str:
    st = st or status()
    lines = [f"AIRUX hardware report — {dt.datetime.now():%Y-%m-%d %H:%M}", "",
             f"Secure Boot: {st['secure_boot']}; live session: {'yes' if st['live'] else 'no'}", ""]
    for f in st["findings"]:
        fix = f" -> fix: {f['fix']['label']}" if f.get("fix") else ""
        lines.append(f"[{f['status']:>14}] {f['title']}: {f['device'] or '-'} — {f['detail']}{fix}")
    lines += ["", "Firmware the kernel could not load:"]
    lines += [f"  {x.get('driver') or '?'} {x.get('address') or ''}: {x['file']}" for x in st["firmware_failures"]] or ["  none"]
    lines += ["", "Devices (bus address, id, driver, name):"]
    lines += [f"  {d['bus']} {d['address']:14} {d['id']:10} {d['driver'] or '(none)':16} {d['name']}" for d in st["devices"]]
    return "\n".join(lines) + "\n"


def save_report() -> str:
    p = Path.home() / f"AIRUX-hardware-report-{dt.datetime.now():%Y%m%d-%H%M}.txt"
    p.write_text(report())
    return str(p)


def helper_cmd(fix: dict) -> list[str]:
    act = fix["action"]
    if act not in ("install", "install_backports", "nvidia", "unblock"):
        raise ValueError(f"no helper action for {act}")
    return ["pkexec", HELPER, act, *fix["args"]]


def find_fix(category: str, st: dict | None = None) -> dict | None:
    st = st or status()
    for f in st["findings"]:
        if f["category"] == category and f.get("fix"):
            return f["fix"]
    return None


class Job:
    """One root-helper run at a time, its output kept for the app to show."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state: dict | None = None

    def start(self, cmd: list[str], title: str) -> dict:
        with self.lock:
            if self.state and not self.state["done"]:
                raise RuntimeError("another fix is still running")
            self.state = {"title": title, "lines": [], "done": False, "result": None, "started": time.time()}
            st = self.state
        threading.Thread(target=self._run, args=(cmd, st), daemon=True).start()
        return st

    def _run(self, cmd: list[str], st: dict) -> None:
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:
                line = line.rstrip()
                if line.startswith("RESULT "):
                    st["result"] = json.loads(line[7:])
                else:
                    st["lines"] = (st["lines"] + [line])[-400:]
            rc = p.wait()
            if st["result"] is None:
                st["result"] = {"ok": False, "error": "cancelled or not authorised" if rc in (126, 127) else f"exit {rc}"}
        except OSError as exc:
            st["result"] = {"ok": False, "error": str(exc)}
        finally:
            st["done"] = True


def run_fix(category: str, log=print) -> dict:
    """Run one category's fix in the foreground (CLI and AIRA)."""
    fix = find_fix(category)
    if fix is None:
        return {"ok": False, "error": f"nothing to fix for {category}"}
    p = subprocess.run(helper_cmd(fix), capture_output=True, text=True)
    result = {"ok": False, "error": f"helper exit {p.returncode}"}
    for line in p.stdout.splitlines():
        if line.startswith("RESULT "):
            result = json.loads(line[7:])
        else:
            log(line)
    result["fix"] = fix
    return result


def fwupd_updates() -> dict:
    if not shutil.which("fwupdmgr"):
        return {"available": False, "devices": []}
    try:
        r = subprocess.run(["fwupdmgr", "get-updates", "--json"], capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"available": True, "devices": [], "note": "could not ask fwupd"}
    devs = []
    for d in data.get("Devices", []):
        rels = d.get("Releases", [])
        if rels:
            devs.append({"name": d.get("Name"), "current": d.get("Version"), "new": rels[0].get("Version"),
                         "summary": rels[0].get("Summary", "")})
    return {"available": True, "devices": devs}


# ---------------------------------------------------------------- the app's server
LAST_USED = [time.time()]


def serve() -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(24)
    job = Job()

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
            if self.path == "/icon.svg":
                return self._send(200, (UI / "airux-drivers.svg").read_bytes(), "image/svg+xml")
            self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            if not self._host_ok() or not secrets.compare_digest(self.headers.get("X-Token", ""), token):
                return self._send(403, b'{"error":"forbidden"}')
            LAST_USED[0] = time.time()
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._send(400, b'{"error":"bad json"}')
            m, a = req.get("method"), req.get("params") or {}
            try:
                if m == "status":
                    out = status()
                elif m == "fix":
                    fix = find_fix(a.get("category", ""))
                    if fix is None:
                        raise ValueError("nothing to fix there")
                    out = {"job": job.start(helper_cmd(fix), fix["label"])}
                elif m == "fwupd":
                    out = fwupd_updates()
                elif m == "fwupd_update":
                    out = {"job": job.start(["pkexec", HELPER, "fwupd_update"], "Device firmware updates")}
                elif m == "job":
                    out = {"job": job.state}
                elif m == "report":
                    out = {"saved": save_report()}
                elif m == "enroll_key":
                    # Secure Boot: mokutil asks for a one-time password, so it runs where the user types it
                    subprocess.Popen(["konsole", "--hold", "-e", "sh", "-c",
                                      "echo 'Choose a one-time password; you will type it again at the next boot "
                                      "(blue MOK Manager screen: Enroll MOK, Continue, Yes, password).'; "
                                      "pkexec mokutil --import /var/lib/dkms/mok.pub && echo 'Done. Restart now.'"],
                                     start_new_session=True)
                    out = {"opened": True}
                else:
                    raise ValueError("unknown method")
                out = {"ok": True, **out}
            except (ValueError, RuntimeError, OSError) as exc:
                out = {"ok": False, "error": str(exc)}
            self._send(200, json.dumps(out, default=str).encode())

        def do_OPTIONS(self):
            self._send(403, b'{"error":"no cross-origin requests"}')

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    d = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "airux-drivers"
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / "ui.token", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"{srv.server_port} {token}\n")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    srv = serve()
    print(f"AIRUX Drivers on http://127.0.0.1:{srv.server_port}/", flush=True)
    while time.time() - LAST_USED[0] < 3600:      # the open window polls; exit after an idle hour
        time.sleep(60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
