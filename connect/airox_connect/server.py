"""airox-connectd: the PC side of AIROX Connect (runs in the user's desktop session).

Two listeners:
  - LAN  https://<PC>:8766 (only while "Allow phone connections" is on): REST + WebSockets for paired
    devices. TLS 1.3, the certificate pinned by the phone, a device token on every request, LAN
    source addresses only, failed attempts rate-limited. Endpoints: docs/CONNECT_PROTOCOL.md.
  - local http://127.0.0.1:<random> (token file ~/.local/state/airox-connect/ui.token): the PC
    window (turn on/off, pairing QR, approve, devices), `airox-connect-send` and AIRA's `phone` tool.

Every phone action runs through AIRA's permission broker (aira.broker.Broker): the connect registry
(tools.py) and the Task Manager's (airux_taskmgr.actions). Confirmations go to the phone that asked
(an action started at the PC is confirmed at the PC); unanswered ones are denied after 120 s; every
action is logged in AIRA's action log in the encrypted vault (or a private file if it is locked).
"""
from __future__ import annotations

import collections
import json
import os
import queue
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aira.broker import Broker
from airux_taskmgr.actions import Confirmations, TaskManager

from . import PROTOCOL, __version__, tools
from .aira_link import AiraError, AiraLink
from .config import APP_PORT, DISCOVERY_PORT, PORT, Settings, downloads_dir, runtime_dir, state_dir
from .files import FilesError, Transfers, list_dir, read_range
from .h264 import Splitter, is_key
from .helpers import Desktop, Stream
from .security import Devices, Limiter, Pairing, ensure_cert, lan_addresses, lan_ok, mac_addresses, server_context
from .ws import BINARY, TEXT, Closed, WebSocket, accept_key

UI = Path(__file__).with_name("ui")
APK = Path(os.environ.get("AIROX_CONNECT_APK", "/usr/share/airox-connect/airox-connect.apk"))
NOTIFY_APPS = {"AIRA", "Phagos", "AIRUX Drivers", "Task Manager", "AIROX Connect", "NEOX"}
VOICE_MAX = 8 * 1024 * 1024
CHUNK = 256 * 1024


def now_us() -> int:
    return time.time_ns() // 1000


def pc_name() -> str:
    return socket.gethostname()


class Conn:
    def __init__(self, ws: WebSocket, device: dict, ip: str):
        self.ws, self.device, self.ip = ws, device, ip
        self.opened = time.time()


class Hub:
    def __init__(self):
        self.conns: dict[str, list[Conn]] = collections.defaultdict(list)
        self.lock = threading.Lock()

    def add(self, c: Conn) -> None:
        with self.lock:
            self.conns[c.device["id"]].append(c)

    def remove(self, c: Conn) -> None:
        with self.lock:
            if c in self.conns.get(c.device["id"], []):
                self.conns[c.device["id"]].remove(c)

    def online(self, dev_id: str) -> bool:
        with self.lock:
            return bool(self.conns.get(dev_id))

    def send(self, dev_id: str, msg: dict) -> int:
        with self.lock:
            targets = list(self.conns.get(dev_id, []))
        text, n = json.dumps(msg, default=str), 0
        for c in targets:
            try:
                c.ws.send_text(text)
                n += 1
            except Closed:
                self.remove(c)
        return n

    def broadcast(self, msg: dict) -> int:
        with self.lock:
            ids = [k for k, v in self.conns.items() if v]
        return sum(self.send(d, msg) for d in ids)

    def close_all(self) -> None:
        with self.lock:
            all_ = [c for v in self.conns.values() for c in v]
            self.conns.clear()
        for c in all_:
            c.ws.close(1001)


def make_sink():
    """AIRA's action log in the encrypted vault, else a private JSON-lines file. Returns (sink, path)."""
    try:
        from aira.config import Config
        from aira.memory.keys import get_key
        from aira.memory.vault import Vault
        key, _ = get_key()
        vault = Vault(Config().vault_path, key)
        return (lambda e: vault.add("action", json.dumps(e, default=str), embed=False)), None
    except Exception:
        p = state_dir() / "actions.jsonl"
        if not p.exists():
            os.close(os.open(p, os.O_WRONLY | os.O_CREAT, 0o600))
        return None, p


class Service:
    def __init__(self, port: int = PORT, discovery_port: int = DISCOVERY_PORT, inbox: Path | None = None,
                 sink=None, log_path: Path | None = None, aira_token: Path | None = None, start_desktop: bool = True):
        self.port, self.discovery_port = port, discovery_port
        self.settings = Settings()
        self.devices = Devices()
        self.pairing = Pairing()
        self.limiter = Limiter()
        self.cert, self.key, self.fp = ensure_cert()
        self.runtime = runtime_dir()
        self.transfers = Transfers(inbox or downloads_dir())
        self.hub = Hub()
        self.ctx = threading.local()
        if sink is None and log_path is None:
            sink, log_path = make_sink()
        self._sink_raw, self._log_path = sink, log_path
        self.recent: collections.deque = collections.deque(maxlen=200)       # actions, for Activity
        self.transcript: collections.deque = collections.deque(maxlen=100)   # AIRA's turns, for Activity
        self.confirms = Confirmations(self._publish_confirm)
        self.registry = tools.build(self)
        self.broker = Broker(self.registry, log_path, confirm=self._confirm, sink=self._sink)
        self.tm = TaskManager(self._tm_confirm, sink=self._sink, log_path=log_path)
        self.desktop = Desktop(self._desktop_event)
        self.start_desktop = start_desktop
        self.aira = AiraLink(self._aira_event, aira_token)
        self.aira_busy = False
        self.lan: ThreadingHTTPServer | None = None
        self.udp: socket.socket | None = None
        self.app_share: ThreadingHTTPServer | None = None
        self.app_share_until = 0.0
        self.local: ThreadingHTTPServer | None = None
        self.local_token = secrets.token_urlsafe(24)
        self.stream_lock = threading.Lock()
        self.stream: Stream | None = None
        self.phone_status: dict[str, dict] = {}
        self.pair_uri: str | None = None

    # ------------------------------------------------------------------ logging and confirmations
    def _sink(self, entry: dict) -> None:
        dev = getattr(self.ctx, "device", None)
        entry = {"via": f"AIROX Connect ({dev['name']})" if dev else "AIROX Connect (PC)", **entry}
        self.recent.append(entry)
        if self._sink_raw is not None:
            self._sink_raw(entry)
        else:
            with self._log_path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")

    def ctx_origin(self) -> str:
        return "phone" if getattr(self.ctx, "device", None) else "pc"

    def _publish_confirm(self, ev: dict) -> None:
        if ev.get("event") == "confirm":
            self.hub.send(ev["device"], {"t": "confirm", **{k: v for k, v in ev.items() if k != "event"}})
        elif ev.get("event") == "confirm_done":
            self.hub.broadcast({"t": "confirm_done", "id": ev["id"], "yes": ev["yes"], "why": ev["why"]})

    def _ask(self, ev: dict) -> bool:
        dev = getattr(self.ctx, "device", None)
        if dev is None:
            if getattr(self.ctx, "preconfirmed", False):    # AIRA's own broker already asked (her phone tool)
                return True
            return pc_confirm(ev.get("summary", ""))
        if not self.hub.online(dev["id"]):
            return False                                     # nobody to ask: denied
        return self.confirms.ask({**ev, "device": dev["id"],
                                  "strong": bool(set(ev.get("risks", [])) & {"delete", "root", "install", "exec"})})

    def _confirm(self, agent: str, tool: str, args: dict, risks) -> bool:
        return self._ask({"source": "connect", "tool": tool, "args": args, "risks": sorted(r.value for r in risks),
                          "summary": tools.describe(tool, args, self)})

    def _tm_confirm(self, ev: dict) -> bool:
        return self._ask({"source": "taskmgr", **ev})

    def run(self, scope: str, tool: str, args: dict, device: dict | None, preconfirmed: bool = False) -> dict:
        self.ctx.device, self.ctx.preconfirmed = device, preconfirmed
        try:
            if scope == "taskmgr":
                return self.tm.run(tool, args)
            d, r = self.broker.execute(tools.CALLER, tool, args)
            return {"ok": r.ok, "allowed": d.allowed, "decision": d.reason, "risks": sorted(x.value for x in d.risks),
                    "data": r.data, "error": r.error}
        finally:
            self.ctx.device, self.ctx.preconfirmed = None, False

    # ------------------------------------------------------------------ events from AIRA and the desktop
    def _aira_event(self, ev: dict) -> None:
        e = ev.get("event")
        if e in ("thinking", "listening"):
            self.aira_busy = True
        elif e == "idle":
            self.aira_busy = False
        if e in ("you", "aira"):
            if not any(t.get("ts") == ev.get("ts") and t.get("text") == ev.get("text") for t in self.transcript):
                self.transcript.append(ev)
        self.hub.broadcast({"t": "aira", **ev})

    def _desktop_event(self, ev: dict) -> None:
        if ev.get("ev") == "notification":
            if ev.get("app") in NOTIFY_APPS or self.settings.get("mirror_all"):
                self.hub.broadcast({"t": "notification", **{k: ev.get(k) for k in ("app", "title", "body", "urgency")}})
        elif ev.get("ev") == "input":
            self.hub.broadcast({"t": "input", "state": ev.get("state"), "error": ev.get("error")})

    # ------------------------------------------------------------------ on / off
    def enable(self, on: bool) -> dict:
        self.settings.set(enabled=bool(on))
        if on:
            self.start_lan()
        else:
            self.stop_lan()
        return self.state()

    def start_lan(self) -> None:
        if self.lan is not None:
            return
        srv = LanServer(("0.0.0.0", self.port), lan_handler(self), server_context(self.cert, self.key), self)
        self.port = srv.server_port
        self.lan = srv
        threading.Thread(target=srv.serve_forever, name="connect-lan", daemon=True).start()
        self._start_discovery()
        self.tm.start()
        self.aira.start()
        if self.start_desktop:
            self.desktop.start()

    def stop_lan(self) -> None:
        if self.lan is not None:
            self.lan.shutdown()
            self.lan.server_close()
            self.lan = None
        if self.udp is not None:
            self.udp.close()
            self.udp = None
        self.hub.close_all()
        self.stop_stream()
        self.pairing.cancel()

    def _start_discovery(self) -> None:
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            u.bind(("0.0.0.0", self.discovery_port))
        except OSError:
            return
        self.udp = u

        def loop():
            while self.udp is u:
                try:
                    data, addr = u.recvfrom(256)
                except OSError:
                    return
                if lan_ok(addr[0]) and data == f"AIROX?{self.devices.pc_id}".encode():
                    try:
                        u.sendto(f"AIROX!{self.devices.pc_id} {self.port}".encode(), addr)
                    except OSError:
                        pass
        threading.Thread(target=loop, name="connect-discovery", daemon=True).start()

    # ------------------------------------------------------------------ pairing
    def pair_start(self) -> dict:
        if self.lan is None:
            self.enable(True)
        code, expires = self.pairing.new_code()
        hosts = ",".join(lan_addresses()) or "127.0.0.1"
        q = urllib.parse.urlencode({"h": hosts, "p": self.port, "fp": self.fp, "c": code, "n": pc_name(),
                                    "id": self.devices.pc_id})
        self.pair_uri = f"airox://pair?{q}"
        return {"uri": self.pair_uri, "expires": expires}

    def pair_request(self, body: dict, ip: str) -> tuple[int, dict]:
        if not self.pairing.claim(str(body.get("code", ""))):
            self.limiter.fail(ip)
            return 403, {"error": "that pairing code is wrong or has expired; show a new QR code on the PC"}
        name = str(body.get("name", "Phone"))[:60]
        p = self.pairing.request({"name": name, "model": str(body.get("model", ""))[:80], "ip": ip,
                                  "platform": str(body.get("platform", "android"))[:20]})
        threading.Thread(target=self._pair_notify, args=(p,), daemon=True).start()
        if not self.pairing.wait(p, 120):
            return 403, {"error": "the PC did not accept this phone"}
        dev_id, token = self.devices.add(name, p["model"], p["platform"])
        self.pair_uri = None
        return 200, {"device_id": dev_id, "token": token, "pc": self.pc_info()}

    def _pair_notify(self, p: dict) -> None:
        yes = pc_confirm(f"Pair {p['name']} ({p['model'] or 'phone'}, {p['ip']}) with this PC? It will be able to "
                         "use AIRA, see what is running and send files.", "Pair", timeout=115)
        if yes is not None:
            self.pairing.answer(p["id"], yes)

    def pc_info(self) -> dict:
        return {"id": self.devices.pc_id, "name": pc_name(), "fingerprint": self.fp, "port": self.port,
                "addresses": lan_addresses(), "macs": mac_addresses(), "version": __version__, "protocol": PROTOCOL,
                "user": os.environ.get("USER", "")}

    def state(self) -> dict:
        return {"enabled": bool(self.lan), "settings": self.settings.all(), "pc": self.pc_info(),
                "devices": [{**d, "online": self.hub.online(d["id"]), "status": self.phone_status.get(d["id"])}
                            for d in self.devices.list()],
                "pairing": {"active": self.pairing.active(), "uri": self.pair_uri if self.pairing.active() else None,
                            "expires": self.pairing.expires,
                            "requests": self.pairing.requests()},
                "app_share": {"on": self.app_share is not None, "until": self.app_share_until, "apk": APK.exists(),
                              "url": [f"http://{a}:{APP_PORT}/airox-connect.apk" for a in lan_addresses()]},
                "aira": self.aira.connected, "input": self.desktop.input_state,
                "stream": bool(self.stream), "transfers": self.transfers.pending_all()}

    # ------------------------------------------------------------------ app sharing (plain HTTP, the APK only)
    def share_app(self, on: bool) -> dict:
        if self.app_share is not None:
            self.app_share.shutdown()
            self.app_share.server_close()
            self.app_share = None
        if on and APK.exists():
            self.app_share = ThreadingHTTPServer(("0.0.0.0", APP_PORT), apk_handler())
            self.app_share_until = time.time() + 600
            threading.Thread(target=self.app_share.serve_forever, daemon=True).start()

            def off(srv=self.app_share):
                time.sleep(600)
                if self.app_share is srv:
                    self.share_app(False)
            threading.Thread(target=off, daemon=True).start()
        return self.state()

    # ------------------------------------------------------------------ activity (what is going on)
    def activity(self) -> dict:
        procs = self.tm.processes()
        apps = [{"name": g["name"], "icon": g["icon"], "cpu": g["cpu"], "memory": g["memory"], "key": g["key"],
                 "count": g["count"]} for g in procs.get("groups", []) if g["kind"] == "app"]
        perf = self.tm.performance()
        return {"apps": apps, "aira": {"connected": self.aira.connected, "busy": self.aira_busy,
                                       "transcript": list(self.transcript)[-30:]},
                "actions": [{k: e.get(k) for k in ("ts", "via", "agent", "tool", "allowed", "ok", "decision", "error")}
                            for e in list(self.recent)[-40:]],
                "summary": summary(perf), "phagos": phagos_status()}

    # ------------------------------------------------------------------ the screen stream
    def stop_stream(self) -> None:
        with self.stream_lock:
            if self.stream:
                self.stream.stop()
                self.stream = None


def summary(perf: dict) -> dict:
    if not perf:
        return {}
    m = perf.get("memory", {})
    g = (perf.get("gpus") or [{}])[0]
    return {"cpu": perf.get("cpu", {}).get("util"), "memory": round(100 * m["used"] / m["total"], 1) if m.get("total") else None,
            "memory_used": m.get("used"), "memory_total": m.get("total"), "gpu": g.get("util"), "gpu_name": g.get("name"),
            "uptime": perf.get("cpu", {}).get("uptime"), "battery": battery()}


def battery() -> dict | None:
    for d in Path("/sys/class/power_supply").glob("BAT*"):
        try:
            cap = int((d / "capacity").read_text())
            st = (d / "status").read_text().strip()
            return {"percent": cap, "status": st}
        except (OSError, ValueError):
            continue
    return None


def phagos_status() -> dict | None:
    sock = Path("/run/phagos/phagos.sock")
    if not sock.exists():
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(str(sock))
        s.sendall(json.dumps({"method": "status", "params": {}}).encode() + b"\n")
        data = s.makefile("rb").readline()
        s.close()
        st = json.loads(data or b"{}")
        return {"protection": st.get("realtime", st.get("protection")), "threats": st.get("threats_total", st.get("threats"))}
    except (OSError, ValueError):
        return None


def pc_confirm(text: str, yes_label: str = "Yes", timeout: float = 120) -> bool | None:
    """Ask at the PC with a desktop notification (Yes / No). None if nobody answered or no notifier."""
    exe = shutil.which("notify-send")
    if not exe:
        return None
    try:
        # normal urgency with an expiry: a critical one would stay on screen after the question is settled
        r = subprocess.run([exe, "-a", "AIROX Connect", "-i", "airox-connect", "-u", "normal", "-t", str(int(timeout * 1000)),
                            "--wait", "-A", f"yes={yes_label}", "-A", "no=No", "AIROX Connect", text],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    a = r.stdout.strip()
    return True if a == "yes" else (False if a == "no" else None)


# ====================================================================== the LAN server
class LanServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, addr, handler, ctx: ssl.SSLContext, svc: Service):
        self.tls, self.svc = ctx, svc
        super().__init__(addr, handler)

    def verify_request(self, request, client_address) -> bool:
        ip = client_address[0]
        return lan_ok(ip) and not self.svc.limiter.blocked(ip)

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (OSError, ssl.SSLError, ValueError)):
            return                       # a phone that went away mid-request: nothing to report
        super().handle_error(request, client_address)

    def finish_request(self, request, client_address) -> None:
        """The TLS handshake happens here, in the connection's own thread (a slow client blocks no one)."""
        try:
            request.settimeout(15)
            tls = self.tls.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
            tls.do_handshake()
        except (ssl.SSLError, OSError):
            try:
                request.close()
            except OSError:
                pass
            return
        tls.settimeout(130)
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            try:
                tls.close()
            except OSError:
                pass


def lan_handler(svc: Service):
    class H(BaseHTTPRequestHandler):
        server_version = "airox-connect"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        @property
        def ip(self) -> str:
            return self.client_address[0]

        def _send(self, code: int, obj=None, body: bytes | None = None, ctype: str = "application/json",
                  headers: dict | None = None) -> None:
            if body is None:
                body = json.dumps(obj if obj is not None else {}, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _auth(self) -> dict | None:
            dev = svc.devices.verify(self.headers.get("Authorization", ""))
            if dev is None:
                svc.limiter.fail(self.ip)
                self._send(401, {"error": "not paired (or this phone was removed on the PC)"})
                return None
            svc.devices.seen(dev["id"], self.ip)
            return dev

        def _json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1024 * 1024:
                raise ValueError("too large")
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _drain(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            while n > 0:
                b = self.rfile.read(min(n, CHUNK))
                if not b:
                    break
                n -= len(b)

        # ---------------------------------------------------------- GET
        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            path = u.path
            if path == "/v1/hello":
                return self._send(200, {"app": "airox-connect", "protocol": PROTOCOL})
            dev = self._auth()
            if dev is None:
                return
            try:
                if path == "/v1/info":
                    return self._send(200, {**svc.pc_info(), "device": {k: dev[k] for k in ("id", "name")},
                                            "aira": svc.aira.connected, "input": svc.desktop.input_state,
                                            "features": ["aira", "voice", "tasks", "files", "clipboard", "links",
                                                         "touchpad", "media", "screen", "second_screen", "power",
                                                         "alerts"]})
                if path == "/v1/status":
                    return self._send(200, {"summary": summary(svc.tm.performance()), "aira": svc.aira.connected,
                                            "busy": svc.aira_busy, "input": svc.desktop.input_state})
                if path == "/v1/activity":
                    return self._send(200, svc.activity())
                m = re.fullmatch(r"/v1/tasks/(processes|performance|services|startup|users)", path)
                if m:
                    fn = {"processes": svc.tm.processes, "performance": svc.tm.performance, "services": svc.tm.services,
                          "startup": svc.tm.startup_apps, "users": svc.tm.users}[m.group(1)]
                    return self._send(200, fn())
                if path == "/v1/files/list":
                    return self._send(200, list_dir(q.get("path", "~"), q.get("hidden") == "1"))
                if path == "/v1/files/incoming":
                    return self._send(200, {"offers": svc.transfers.pending(dev["id"], origin="pc")})
                m = re.fullmatch(r"/v1/files/hash/([A-Za-z0-9_-]+)", path)
                if m:
                    return self._send(200, {"sha256": svc.transfers.offer_hash(dev["id"], m.group(1))})
                m = re.fullmatch(r"/v1/files/download/([A-Za-z0-9_-]+)", path)
                if m:
                    return self._download(dev, m.group(1))
                m = re.fullmatch(r"/v1/files/upload/([A-Za-z0-9_-]+)", path)
                if m:
                    return self._send(200, {"received": svc.transfers.upload_status(dev["id"], m.group(1))})
                if path == "/v1/clipboard":
                    r = svc.run("connect", "clipboard_get", {}, dev)
                    return self._send(200 if r["ok"] else 500, r)
                if path == "/v1/screen/shot":
                    r = svc.run("connect", "screenshot", {}, dev)
                    if not r["ok"]:
                        return self._send(500, r)
                    return self._send(200, body=Path(r["data"]["path"]).read_bytes(), ctype="image/jpeg")
                if path == "/v1/aira/speech":
                    return self._speech(q.get("name", ""))
                if path == "/v1/ws":
                    return self._ws_events(dev)
                if path == "/v1/screen/stream":
                    return self._ws_stream(dev, q)
            except FilesError as exc:
                return self._send(400, {"error": str(exc)})
            self._send(404, {"error": "not found"})

        do_HEAD = do_GET

        def _download(self, dev: dict, tid: str) -> None:
            o = svc.transfers.get_offer(dev["id"], tid)
            p = Path(o["path"])
            size = p.stat().st_size
            if size == 0:
                return self._send(200, body=b"", ctype=o["mime"])
            rng = self.headers.get("Range")
            start, end = read_range(p, rng, size)
            n = end - start + 1
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", o["mime"])
            self.send_header("Content-Length", str(n))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(o["name"]))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with p.open("rb") as f:
                f.seek(start)
                left = n
                while left > 0:
                    b = f.read(min(CHUNK, left))
                    if not b:
                        break
                    self.wfile.write(b)
                    left -= len(b)
            if end == size - 1:
                o["sent"] = size

        def _speech(self, name: str) -> None:
            if not re.fullmatch(r"aira-say-\d+\.wav", name):
                return self._send(400, {"error": "bad name"})
            try:
                from aira.config import Config
                f = Config().data_dir / "speech" / name
            except Exception:
                return self._send(404, {"error": "AIRA's voice is not on this PC"})
            if not f.is_file():
                return self._send(404, {"error": "gone"})
            self._send(200, body=f.read_bytes(), ctype="audio/wav")

        # ---------------------------------------------------------- POST / PUT
        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            try:
                if path == "/v1/pair":
                    if not svc.pairing.active():
                        svc.limiter.fail(self.ip)
                        self._drain()
                        return self._send(403, {"error": "pairing is not open on the PC"})
                    code, out = svc.pair_request(self._json(), self.ip)
                    return self._send(code, out)
                dev = self._auth()
                if dev is None:
                    return self._drain()
                if path == "/v1/run":
                    b = self._json()
                    scope, tool, args = b.get("scope", "connect"), str(b.get("tool", "")), b.get("args") or {}
                    if scope not in ("connect", "taskmgr") or not isinstance(args, dict):
                        return self._send(400, {"error": "bad request"})
                    if tool == "send_files":
                        args = {**args, "device": dev["id"]}        # only ever to the asking device
                    rid = secrets.token_hex(6)

                    def work():
                        out = svc.run(scope, tool, args, dev)
                        svc.hub.send(dev["id"], {"t": "result", "rid": rid, "scope": scope, "tool": tool, **out})
                    threading.Thread(target=work, daemon=True).start()
                    return self._send(202, {"rid": rid})
                m = re.fullmatch(r"/v1/files/done/([A-Za-z0-9_-]+)", path)
                if m:
                    svc.transfers.done(dev["id"], m.group(1))
                    return self._send(200, {"ok": True})
                if path == "/v1/aira/voice":
                    return self._voice(dev)
            except (ValueError, KeyError):
                return self._send(400, {"error": "bad request"})
            except FilesError as exc:
                return self._send(400, {"error": str(exc)})
            self._drain()
            self._send(404, {"error": "not found"})

        def _voice(self, dev: dict) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            if not 44 < n <= VOICE_MAX:
                self._drain()
                return self._send(400, {"error": "send a WAV recording of up to 8 MB"})
            data = self.rfile.read(n)
            if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                return self._send(400, {"error": "not a WAV file"})
            f = svc.runtime / f"voice-{secrets.token_hex(6)}.wav"
            fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            try:
                svc.aira.voice(f, quiet_pc=bool(svc.settings.get("pc_silent")))
            except AiraError as exc:
                f.unlink(missing_ok=True)
                return self._send(503, {"error": str(exc)})
            self._send(202, {"ok": True})

        def do_PUT(self):
            path = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(path.query))
            m = re.fullmatch(r"/v1/files/upload/([A-Za-z0-9_-]+)", path.path)
            dev = self._auth()
            if dev is None:
                return self._drain()
            if not m:
                self._drain()
                return self._send(404, {"error": "not found"})
            uid = m.group(1)
            try:
                name, total, sha = q.get("name", ""), int(q.get("size", "-1")), q.get("sha256", "")
                start = int(q.get("start", "0"))
                n = int(self.headers.get("Content-Length") or 0)
                if start == 0 and svc.transfers.upload_status(dev["id"], uid) == 0:
                    r = svc.run("connect", "receive_file", {"name": name[:255], "size": str(total)}, dev)
                    if not r["allowed"]:
                        self._drain()
                        return self._send(403, r)

                def chunks():
                    left = n
                    while left > 0:
                        b = self.rfile.read(min(CHUNK, left))
                        if not b:
                            raise FilesError("the connection broke")
                        left -= len(b)
                        yield b
                out = svc.transfers.upload_chunk(dev["id"], uid, name, total, sha, start, chunks())
            except FilesError as exc:
                self.close_connection = True
                return self._send(409 if "resume at" in str(exc) else 400, {"error": str(exc)})
            except ValueError:
                self.close_connection = True
                return self._send(400, {"error": "bad request"})
            if out.get("complete"):
                svc.hub.send(dev["id"], {"t": "saved", "name": out["name"], "path": out["saved"]})
                svc.recent.append({"ts": time.time(), "via": f"AIROX Connect ({dev['name']})", "tool": "receive_file",
                                   "allowed": True, "ok": True, "decision": f"saved {out['name']}"})
            self._send(200, out)

        # ---------------------------------------------------------- WebSockets
        def _upgrade(self) -> WebSocket | None:
            key = self.headers.get("Sec-WebSocket-Key", "")
            if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
                self._send(400, {"error": "WebSocket upgrade expected"})
                return None
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept_key(key))
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            self.connection.settimeout(None)
            return WebSocket(self.rfile, self.wfile)

        def _ws_events(self, dev: dict) -> None:
            ws = self._upgrade()
            if ws is None:
                return
            c = Conn(ws, dev, self.ip)
            svc.hub.add(c)
            try:
                ws.send_text(json.dumps({"t": "hello", "pc": svc.pc_info(), "aira": svc.aira.connected,
                                         "busy": svc.aira_busy, "input": svc.desktop.input_state, "time": now_us(),
                                         "transcript": list(svc.transcript)[-40:]}, default=str))
                for ev in svc.confirms.events():
                    if ev.get("device") == dev["id"]:
                        ws.send_text(json.dumps({"t": "confirm", **{k: v for k, v in ev.items() if k != "event"}}, default=str))
                for o in svc.transfers.pending(dev["id"], origin="pc"):
                    ws.send_text(json.dumps({"t": "incoming", "files": [o]}))
                while True:
                    op, data = ws.recv()
                    if op != TEXT:
                        continue
                    try:
                        m = json.loads(data)
                    except ValueError:
                        continue
                    self._ws_message(dev, ws, m)
            except Closed:
                pass
            finally:
                svc.hub.remove(c)

        def _ws_message(self, dev: dict, ws: WebSocket, m: dict) -> None:
            t = m.get("t")
            if t == "ping":
                ws.send_text(json.dumps({"t": "pong", "ts": m.get("ts"), "server": now_us()}))
            elif t == "say":
                text = str(m.get("text", "")).strip()[:2000]
                if text:
                    try:
                        svc.aira.command(text)
                    except AiraError as exc:
                        ws.send_text(json.dumps({"t": "aira", "event": "error", "text": str(exc)}))
            elif t == "answer":
                cid, yes = str(m.get("id", "")), bool(m.get("yes"))
                if m.get("src") == "aira":
                    try:
                        svc.aira.confirm(cid, yes)
                    except AiraError as exc:
                        ws.send_text(json.dumps({"t": "aira", "event": "error", "text": str(exc)}))
                else:
                    ev = next((e for e in svc.confirms.events() if e["id"] == cid), None)
                    if ev is not None and ev.get("device") == dev["id"]:     # only the device that was asked
                        svc.confirms.answer(cid, yes)
            elif t == "input":
                if svc.desktop.input_state != "ready":
                    if m.get("op") == "start":
                        threading.Thread(target=svc.run, args=("connect", "remote_input", {"action": "start"}, dev),
                                         daemon=True).start()
                    return
                op = m.get("op")
                if op in ("pointer", "button", "scroll", "key", "text"):
                    svc.desktop.send({k: v for k, v in m.items() if k != "t"})
            elif t == "status":
                svc.phone_status[dev["id"]] = {"battery": m.get("battery"), "charging": m.get("charging"),
                                               "time": time.time()}

        def _ws_stream(self, dev: dict, q: dict) -> None:
            source = q.get("source", "virtual")
            if source not in ("virtual", "monitor"):
                return self._send(400, {"error": "source is virtual or monitor"})
            fps = max(10, min(int(q.get("fps", "60")), 120))
            kbps = max(1000, min(int(q.get("kbps", "12000")), 60000))
            r = svc.run("connect", "screen_stream", {"source": source}, dev)
            if not r["allowed"]:
                return self._send(403, r)
            svc.stop_stream()                       # one stream at a time
            ws = self._upgrade()
            if ws is None:
                return
            info: dict = {}

            def on_status(ev):
                if ev.get("ev") == "stream":
                    info.update(ev)
                    if ev.get("virtual") and ev.get("output"):
                        svc.desktop.send({"op": "place_virtual", "output": ev["output"]})
                    svc.desktop.send({"op": "input_start"})
                try:
                    ws.send_text(json.dumps({"t": "stream", **ev}))
                except Closed:
                    pass
            st = Stream(source, fps, kbps, on_status)
            with svc.stream_lock:
                svc.stream = st
            try:
                proc = st.start()
            except RuntimeError as exc:
                ws.send_text(json.dumps({"t": "stream", "ev": "error", "error": str(exc)}))
                return ws.close()
            frames: queue.Queue = queue.Queue()
            backlog_max = max(fps // 4, 4)

            def reader():
                sp = Splitter()
                while True:
                    b = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
                    if not b:
                        break
                    for au in sp.feed(b):
                        frames.put(au)
                frames.put(None)

            def sender():
                skipping = False
                sent = 0
                while True:
                    au = frames.get()
                    if au is None:
                        break
                    if frames.qsize() > backlog_max:
                        skipping = True             # the phone is behind: wait for the next key frame
                    if skipping:
                        if not is_key(au):
                            continue
                        skipping = False
                    try:
                        ws.send_binary(struct.pack("!Q", now_us()) + au)
                        sent += 1
                    except Closed:
                        break
                st.stop()
            threading.Thread(target=reader, daemon=True).start()
            threading.Thread(target=sender, daemon=True).start()
            try:
                while True:
                    op, data = ws.recv()
                    if op != TEXT:
                        continue
                    try:
                        m = json.loads(data)
                    except ValueError:
                        continue
                    t = m.get("t")
                    if t == "ping":
                        ws.send_text(json.dumps({"t": "pong", "ts": m.get("ts"), "server": now_us()}))
                    elif t == "touch" and info.get("output"):
                        svc.desktop.send({"op": "abs", "output": info["output"], "x": float(m.get("x", 0)),
                                          "y": float(m.get("y", 0)), "phase": m.get("phase", "move"),
                                          "press": bool(m.get("press", True))})
                    elif t in ("button", "scroll", "key", "text"):
                        svc.desktop.send({"op": t, **{k: v for k, v in m.items() if k != "t"}})
            except Closed:
                pass
            finally:
                st.stop()
                with svc.stream_lock:
                    if svc.stream is st:
                        svc.stream = None

        def do_OPTIONS(self):
            self._send(405, {"error": "no"})
    return H


def apk_handler():
    class A(BaseHTTPRequestHandler):
        server_version = "airox-connect"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if not lan_ok(self.client_address[0]) or self.path != "/airox-connect.apk" or not APK.exists():
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = APK.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", 'attachment; filename="airox-connect.apk"')
            self.end_headers()
            self.wfile.write(data)
    return A


# ====================================================================== the local server (PC window, CLI, AIRA)
def local_handler(svc: Service):
    class L(BaseHTTPRequestHandler):
        server_version = "airox-connect-local"

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
            p = svc.local.server_port
            return self.headers.get("Host") in (f"127.0.0.1:{p}", f"localhost:{p}")

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b'{"error":"bad host"}')
            path = self.path.split("?")[0]
            if path == "/":
                return self._send(200, (UI / "index.html").read_text().replace("__TOKEN__", svc.local_token).encode(),
                                  "text/html; charset=utf-8")
            if path == "/logo.svg":
                return self._send(200, (UI / "airox-connect.svg").read_bytes(), "image/svg+xml")
            if path in ("/qr.svg", "/app-qr.svg"):
                data = svc.pair_uri if path == "/qr.svg" else (svc.state()["app_share"]["url"] or [""])[0]
                if not data or (path == "/qr.svg" and not svc.pairing.active()):
                    return self._send(404, b"{}")
                return self._send(200, qr_svg(data), "image/svg+xml")
            self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            if not self._host_ok() or not secrets.compare_digest(self.headers.get("X-Token", ""), svc.local_token):
                return self._send(403, b'{"error":"forbidden"}')
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._send(400, b'{"error":"bad json"}')
            m, a = req.get("method"), req.get("params") or {}
            try:
                if m == "state":
                    out = svc.state()
                elif m == "enable":
                    out = svc.enable(bool(a.get("on")))
                elif m == "pair_start":
                    out = svc.pair_start()
                elif m == "pair_cancel":
                    svc.pairing.cancel()
                    out = {"ok": True}
                elif m == "approve":
                    out = {"ok": svc.pairing.answer(str(a.get("id", "")), bool(a.get("yes")))}
                elif m == "revoke":
                    dev_id = str(a.get("id", ""))
                    for c in list(svc.hub.conns.get(dev_id, [])):
                        c.ws.close(4001)
                    out = {"ok": svc.devices.revoke(dev_id)}
                elif m == "settings":
                    out = svc.settings.set(**{k: a[k] for k in ("mirror_all", "pc_silent", "name") if k in a})
                elif m == "share_app":
                    out = svc.share_app(bool(a.get("on")))
                elif m == "send":
                    devs = [a["device"]] if a.get("device") else [d["id"] for d in svc.devices.list()
                                                                    if svc.hub.online(d["id"])]
                    if not devs:
                        out = {"ok": False, "error": "no paired phone is connected"}
                    else:
                        results = []
                        for d in devs:
                            r = svc.run("connect", "send_files", {"paths": a.get("paths", []), "device": d}, None,
                                        preconfirmed=bool(a.get("confirmed_by_aira")))
                            if r["ok"]:
                                svc.hub.send(d, {"t": "incoming", "files": r["data"]["offers"]})
                            results.append({"device": d, **r})
                        out = {"ok": all(r["ok"] for r in results), "results": results}
                elif m == "ring":
                    devs = [a["device"]] if a.get("device") else [d["id"] for d in svc.devices.list()]
                    n = sum(svc.hub.send(d, {"t": "ring"}) for d in devs)
                    out = {"ok": n > 0, "rung": n, "error": None if n else "no paired phone is connected"}
                elif m == "phone_status":
                    out = {"phones": [{**d, "online": svc.hub.online(d["id"]), "status": svc.phone_status.get(d["id"])}
                                      for d in svc.devices.list()]}
                else:
                    return self._send(400, b'{"error":"unknown method"}')
            except (KeyError, TypeError, ValueError) as exc:
                return self._send(400, json.dumps({"error": str(exc)}).encode())
            self._send(200, json.dumps(out, default=str).encode())

        def do_OPTIONS(self):
            self._send(403, b'{"error":"no cross-origin requests"}')
    return L


def qr_svg(data: str) -> bytes:
    exe = shutil.which("qrencode")
    if not exe:
        return b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>'
    r = subprocess.run([exe, "-t", "SVG", "-m", "2", "-s", "8", "--foreground=0b1016", "--background=ffffff", "-o", "-",
                        data], capture_output=True, timeout=10)
    return r.stdout


def serve_local(svc: Service, port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), local_handler(svc))
    srv.daemon_threads = True
    svc.local = srv
    f = state_dir() / "ui.token"
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{srv.server_port} {svc.local_token}\n")
    threading.Thread(target=srv.serve_forever, name="connect-local", daemon=True).start()
    return srv


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="airox-connectd")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args(argv)
    svc = Service(port=a.port)
    serve_local(svc)
    if svc.settings.get("enabled"):
        try:
            svc.start_lan()
        except OSError as exc:
            print(f"airox-connectd: cannot listen on port {a.port}: {exc}", file=sys.stderr, flush=True)
    print(f"airox-connectd: local window on 127.0.0.1:{svc.local.server_port}; phones "
          f"{'allowed on port %d' % svc.port if svc.lan else 'off'}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    svc.stop_lan()
    return 0


if __name__ == "__main__":
    sys.exit(main())
