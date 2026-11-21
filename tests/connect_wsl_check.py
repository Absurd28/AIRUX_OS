#!/usr/bin/env python3
"""AIROX Connect check in WSL (no desktop): a reference client speaks the real protocol to airox-connectd.

airox-connectd runs with the real AIRA shell server (aira.shell.server.Shell) in front of a stand-in
orchestrator, a temporary home folder and temporary state. Pass requires every line:
  pair          QR link from the local API; TLS pinned to its fingerprint; the PC approves; token issued
  refuse        no token, a wrong token, a reused or wrong code, a revoked device, a non-LAN address,
                and a wrong pin (the client refuses the certificate) are all refused
  info          /v1/info, /v1/status, /v1/activity answer
  aira          a typed command over the WebSocket gets AIRA's reply; a spoken WAV goes through the
                shell's /voice_file and is answered; AIRA's own confirmation is answered from the phone
  tasks         the process list holds a test process; ending it is confirmed on the phone (no -> still
                running, yes -> ended and read back)
  files         resumable upload with SHA-256 (second upload of the same name gets a new name);
                PC->phone offer confirmed on the phone, then a ranged download matching the SHA-256
  discovery     UDP discovery answers only with the right PC id
  ratelimit     ten failures from one address block it
Run as the normal user: /srv/airux-work/venv/bin/python tests/connect_wsl_check.py
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for p in (REPO, REPO / "taskmgr", REPO / "connect"):
    sys.path.insert(0, str(p))

TMP = Path(tempfile.mkdtemp(prefix="airox-check-"))
HOME = TMP / "home"
for d in ("home/Pictures", "state", "run"):
    (TMP / d).mkdir(parents=True)
os.environ.update(XDG_STATE_HOME=str(TMP / "state"), XDG_RUNTIME_DIR=str(TMP / "run"), HOME=str(HOME))
os.chmod(TMP / "run", 0o700)
(HOME / "Pictures" / "holiday.jpg").write_bytes(os.urandom(2_500_000))

from aira.shell.server import Shell  # noqa: E402
from aira.tools.base import Risk  # noqa: E402
from airox_connect import ws as W  # noqa: E402
from airox_connect.server import Service, serve_local  # noqa: E402

results: dict[str, bool] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    results[name] = results.get(name, True) and bool(ok)
    print(f"  {'ok ' if ok else 'FAIL'} {name}: {detail}", flush=True)


# ------------------------------------------------------------------ a stand-in AIRA behind the real shell server
class Call:
    def __init__(self, tool, args):
        self.tool, self.args, self.verified = tool, args, True


class Run:
    def __init__(self, agent, calls):
        self.agent, self.tool_calls = agent, calls


class Turn:
    def __init__(self, reply, runs=()):
        self.reply, self.kind, self.runs = reply, "task", list(runs)


class FakeOrch:
    def __init__(self, shell):
        self.listeners, self.shell, self.confirmed = [], shell, None

    def handle(self, text):
        if "delete" in text:
            self.confirmed = self.shell.confirm("desktop", "delete_path", {"path": "~/old.txt"}, {Risk.DELETE})
            return Turn("Deleted ~/old.txt. Verified: it is in the trash." if self.confirmed else "Not done: you said no.")
        return Turn("You have 41.2 GiB free in your home folder. Verified: disk_usage.",
                    [Run("system", [Call("disk_usage", {"path": "~"})])])


class FakeSpeaker:
    play = True


class FakeVoice:
    speaker = FakeSpeaker()

    def __init__(self, orch):
        self.orch = orch

    def handle_audio(self, wav):
        class VT:
            pass
        vt = VT()
        vt.transcript = "how much disk space do I have"
        vt.turn = self.orch.handle(vt.transcript)
        vt.speech = {"spoken": FakeSpeaker.play, "played_on_pc": self.speaker.play}
        return vt


shell = Shell(port=0, token_file=TMP / "state" / "aira" / "shell.token")
shell.serve()
orch = FakeOrch(shell)
shell.attach(orch, FakeVoice(orch))

# ------------------------------------------------------------------ airox-connectd
svc = Service(port=0, discovery_port=0, inbox=HOME / "Downloads" / "AIROX Connect", log_path=TMP / "actions.jsonl",
              start_desktop=False)
serve_local(svc)
LOCAL_PORT, LOCAL_TOKEN = svc.local.server_port, svc.local_token


def local(method, **params):
    req = urllib.request.Request(f"http://127.0.0.1:{LOCAL_PORT}/api", data=json.dumps({"method": method, "params": params}).encode(),
                                 headers={"Content-Type": "application/json", "X-Token": LOCAL_TOKEN})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


# ------------------------------------------------------------------ the reference client (what the app does)
class Client:
    def __init__(self, host, port, fp, token=None):
        self.host, self.port, self.fp, self.token = host, port, fp, token
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE          # trust comes from the pin, checked below
        self.ctx.minimum_version = ssl.TLSVersion.TLSv1_3

    def sock(self):
        s = self.ctx.wrap_socket(socket.create_connection((self.host, self.port), timeout=30))
        got = hashlib.sha256(s.getpeercert(binary_form=True)).hexdigest()
        if got != self.fp:
            s.close()
            raise ssl.SSLError(f"certificate pin mismatch ({got[:12]}...)")
        return s

    def req(self, method, path, body=None, headers=None, auth=True, raw=False, timeout=150):
        c = http.client.HTTPSConnection(self.host, self.port, context=self.ctx, timeout=timeout)
        c.connect()
        got = hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest()
        if got != self.fp:
            c.close()
            raise ssl.SSLError("certificate pin mismatch")
        h = dict(headers or {})
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        if data is not None and "Content-Type" not in h:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        if raw:
            return r.status, out, dict(r.getheaders())
        return r.status, (json.loads(out) if out else {})

    def ws(self, path):
        s = self.sock()
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        s.sendall((f"GET {path} HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer {self.token}\r\n\r\n").encode())
        f = s.makefile("rb")
        status = f.readline()
        while f.readline() not in (b"\r\n", b""):
            pass
        assert b" 101 " in status, status
        return WSClient(s, f)


class WSClient:
    def __init__(self, s, f):
        self.s, self.f = s, f
        self.inbox: list[dict] = []
        self.cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def send(self, obj):
        self.s.sendall(W.encode(W.TEXT, json.dumps(obj).encode(), mask=os.urandom(4)))

    def _read(self):
        try:
            while True:
                b1, b2 = self.f.read(2)
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack("!H", self.f.read(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", self.f.read(8))[0]
                data = self.f.read(n)
                if b1 & 0x0F == W.TEXT:
                    with self.cv:
                        self.inbox.append(json.loads(data))
                        self.cv.notify_all()
                elif b1 & 0x0F == W.CLOSE:
                    return
        except (OSError, ValueError, struct.error):
            return

    def wait(self, pred, timeout=20):
        end = time.time() + timeout
        with self.cv:
            while time.time() < end:
                for m in self.inbox:
                    if pred(m):
                        self.inbox.remove(m)
                        return m
                self.cv.wait(0.2)
        return None


def main() -> int:
    print("== enable and pair", flush=True)
    local("enable", on=True)
    port = svc.port
    pair = local("pair_start")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(pair["uri"]).query))
    check("pair", q["fp"] == svc.fp and q["c"] and q["p"] == str(port), f"QR {pair['uri'][:60]}...")
    cl = Client("127.0.0.1", port, q["fp"])

    def approver():
        for _ in range(100):
            reqs = local("state")["pairing"]["requests"]
            if reqs:
                local("approve", id=reqs[0]["id"], yes=True)
                return
            time.sleep(0.1)
    threading.Thread(target=approver, daemon=True).start()
    st, out = cl.req("POST", "/v1/pair", {"code": q["c"], "name": "Test Pixel", "model": "Pixel 8", "platform": "android"}, auth=False)
    check("pair", st == 200 and "token" in out and out["pc"]["fingerprint"] == svc.fp, f"status {st}, device {out.get('device_id')}")
    cl.token = f"{out['device_id']}.{out['token']}"
    dev_id = out["device_id"]

    print("== refused", flush=True)
    st, _ = cl.req("POST", "/v1/pair", {"code": q["c"], "name": "Again"}, auth=False)
    check("refuse", st == 403, f"reused code -> {st}")
    st, _ = cl.req("GET", "/v1/info", auth=False)
    check("refuse", st == 401, f"no token -> {st}")
    bad = Client("127.0.0.1", port, q["fp"], token=f"{dev_id}.wrong")
    check("refuse", bad.req("GET", "/v1/info")[0] == 401, "wrong token -> 401")
    try:
        Client("127.0.0.1", port, "00" * 32, token=cl.token).req("GET", "/v1/info")
        check("refuse", False, "a wrong pin was accepted")
    except ssl.SSLError:
        check("refuse", True, "wrong pin -> client refuses the certificate")
    check("refuse", svc.lan.verify_request(None, ("8.8.8.8", 4444)) is False
          and svc.lan.verify_request(None, ("100.64.0.9", 4444)) is False, "public and CGNAT addresses refused")
    v = TMP / "evil.wav"
    v.write_bytes(b"RIFF....WAVE")
    try:
        r = urllib.request.Request(f"http://127.0.0.1:{shell.port}/voice_file", data=json.dumps({"path": str(v)}).encode(),
                                   headers={"X-AIRA-Token": shell.token, "Content-Type": "application/json"})
        urllib.request.urlopen(r, timeout=10)
        check("refuse", False, "shell ran a WAV from outside AIROX Connect's folder")
    except urllib.error.HTTPError as exc:
        check("refuse", exc.code == 400, f"shell /voice_file outside the runtime folder -> {exc.code}")

    print("== info", flush=True)
    st, info = cl.req("GET", "/v1/info")
    check("info", st == 200 and info["device"]["name"] == "Test Pixel" and "second_screen" in info["features"],
          f"pc {info.get('name')}, features {len(info.get('features', []))}")
    time.sleep(1.5)
    st, s = cl.req("GET", "/v1/status")
    check("info", st == 200 and s["summary"].get("memory_total"), f"cpu {s['summary'].get('cpu')}%, mem {s['summary'].get('memory')}%")
    st, act = cl.req("GET", "/v1/activity")
    check("info", st == 200 and "apps" in act and "actions" in act, f"{len(act.get('actions', []))} actions logged")

    print("== AIRA", flush=True)
    w = cl.ws("/v1/ws")
    hello = w.wait(lambda m: m["t"] == "hello")
    check("aira", hello is not None, "events WebSocket open")
    w.wait(lambda m: m["t"] == "aira" and m.get("event") == "link", 8)
    w.send({"t": "say", "text": "how much disk space do I have"})
    you = w.wait(lambda m: m["t"] == "aira" and m.get("event") == "you")
    rep = w.wait(lambda m: m["t"] == "aira" and m.get("event") == "aira")
    check("aira", you and you.get("via") == "phone" and rep and "41.2 GiB" in rep["text"] and "verified" in rep["tags"],
          f"reply: {rep and rep['text'][:50]}")
    w.wait(lambda m: m["t"] == "aira" and m.get("event") == "idle")
    buf = TMP / "say.wav"
    with wave.open(str(buf), "wb") as wv:
        wv.setnchannels(1), wv.setsampwidth(2), wv.setframerate(16000), wv.writeframes(b"\x00\x00" * 16000)
    st, _ = cl.req("POST", "/v1/aira/voice", buf.read_bytes(), headers={"Content-Type": "audio/wav"})
    you = w.wait(lambda m: m["t"] == "aira" and m.get("event") == "you")
    spoken = w.wait(lambda m: m["t"] == "aira" and m.get("event") == "spoken")
    check("aira", st == 202 and you and you["via"] == "phone (voice)" and spoken and spoken["voice"]["played_on_pc"] is False,
          f"voice -> '{you and you['text']}', PC kept quiet: {spoken and not spoken['voice']['played_on_pc']}")
    w.wait(lambda m: m["t"] == "aira" and m.get("event") == "idle")
    w.send({"t": "say", "text": "delete old.txt"})
    ask = w.wait(lambda m: m["t"] == "aira" and m.get("event") == "confirm")
    if ask:
        w.send({"t": "answer", "src": "aira", "id": ask["id"], "yes": True})
    rep = w.wait(lambda m: m["t"] == "aira" and m.get("event") == "aira" and m["text"].startswith(("Deleted", "Not done")))
    check("aira", ask is not None and orch.confirmed is True and rep and rep["text"].startswith("Deleted"),
          f"AIRA's confirmation answered from the phone: {orch.confirmed}; asked {ask and ask.get('tool')}, "
          f"reply {rep and rep.get('text', '')[:40]!r}"
          + ("" if ask else f" (no confirm event; unread: {[(m['t'], m.get('event'), m.get('text', '')[:40]) for m in w.inbox][-8:]})"))

    print("== Task Manager", flush=True)
    kids = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"]) for _ in range(2)]
    time.sleep(2.2)
    st, pr = cl.req("GET", "/v1/tasks/processes")
    procs = {p["pid"]: p for g in pr["groups"] for p in g["procs"]}
    check("tasks", all(k.pid in procs for k in kids), f"{len(procs)} processes, test processes listed")
    for kid, yes in ((kids[0], False), (kids[1], True)):
        st, r = cl.req("POST", "/v1/run", {"scope": "taskmgr", "tool": "end_task",
                                          "args": {"pid": kid.pid, "start": procs[kid.pid]["start"]}})
        ask = w.wait(lambda m: m["t"] == "confirm" and m.get("source") == "taskmgr")
        w.send({"t": "answer", "id": ask["id"], "yes": yes})
        res = w.wait(lambda m: m["t"] == "result" and m["rid"] == r["rid"])
        time.sleep(0.3)
        if yes:
            check("tasks", res["ok"] and res["data"]["verified"] and kid.poll() is not None, f"yes -> {res['data']['detail']}")
        else:
            check("tasks", not res["allowed"] and kid.poll() is None, f"no -> {res['decision']}, still running")
    for k in kids:
        if k.poll() is None:
            k.kill()

    print("== files", flush=True)
    st, ls = cl.req("GET", "/v1/files/list?path=" + urllib.parse.quote("~/Pictures"))
    check("files", st == 200 and ls["items"][0]["name"] == "holiday.jpg", f"{len(ls['items'])} item(s) in ~/Pictures")
    data = os.urandom(3_000_000)
    sha = hashlib.sha256(data).hexdigest()

    def put(uid, start, chunk, name="sunset.jpg", total=len(data), digest=sha):
        qs = urllib.parse.urlencode({"name": name, "size": total, "sha256": digest, "start": start})
        return cl.req("PUT", f"/v1/files/upload/{uid}?{qs}", chunk, headers={"Content-Type": "application/octet-stream"})
    put("u-000000001", 0, data[:1_000_000])
    st, have = cl.req("GET", "/v1/files/upload/u-000000001")
    st2, out = put("u-000000001", have["received"], data[have["received"]:])
    saved = HOME / "Downloads" / "AIROX Connect" / "sunset.jpg"
    check("files", have["received"] == 1_000_000 and out.get("complete") and saved.read_bytes() == data,
          f"upload resumed at {have['received']}, saved {out.get('name')}")
    st, out2 = put("u-000000002", 0, b"abc", total=3, digest=hashlib.sha256(b"abc").hexdigest())
    check("files", out2.get("name") == "sunset (1).jpg" and saved.read_bytes() == data, f"same name -> {out2.get('name')}")
    st, r = cl.req("POST", "/v1/run", {"scope": "connect", "tool": "send_files", "args": {"paths": ["~/Pictures/holiday.jpg"]}})
    ask = w.wait(lambda m: m["t"] == "confirm" and m.get("tool") == "send_files")
    w.send({"t": "answer", "id": ask["id"], "yes": True})
    res = w.wait(lambda m: m["t"] == "result" and m["rid"] == r["rid"])
    tid = res["data"]["offers"][0]["tid"]
    s1, p1, h1 = cl.req("GET", f"/v1/files/download/{tid}", headers={"Range": "bytes=0-999999"}, raw=True)
    s2, p2, h2 = cl.req("GET", f"/v1/files/download/{tid}", headers={"Range": "bytes=1000000-"}, raw=True)
    st, hh = cl.req("GET", f"/v1/files/hash/{tid}")
    got = hashlib.sha256(p1 + p2).hexdigest()
    check("files", s1 == 206 and s2 == 206 and got == hh["sha256"] == hashlib.sha256((HOME / "Pictures" / "holiday.jpg").read_bytes()).hexdigest(),
          f"offer confirmed ('{ask['summary']}'), ranged download, SHA-256 {got[:12]}... matches")
    st, _ = cl.req("GET", "/v1/files/list?path=" + urllib.parse.quote("~/../"))
    check("files", st == 400, f"path outside home -> {st}")

    print("== discovery", flush=True)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(3)
    dport = svc.udp.getsockname()[1]
    u.sendto(b"AIROX?wrongid", ("127.0.0.1", dport))
    try:
        u.recvfrom(100)
        check("discovery", False, "answered a wrong id")
    except socket.timeout:
        check("discovery", True, "wrong id: no answer")
    u.sendto(f"AIROX?{svc.devices.pc_id}".encode(), ("127.0.0.1", dport))
    ans = u.recvfrom(100)[0].decode()
    check("discovery", ans == f"AIROX!{svc.devices.pc_id} {port}", ans)

    print("== revoke and rate limit", flush=True)
    local("revoke", id=dev_id)
    check("refuse", cl.req("GET", "/v1/info")[0] == 401, "revoked device -> 401")
    for _ in range(10):
        try:
            bad.req("GET", "/v1/info")
        except (OSError, ssl.SSLError, http.client.HTTPException):
            pass
    try:
        bad.req("GET", "/v1/info")
        check("ratelimit", False, "still answering after 10 failures")
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        check("ratelimit", True, f"blocked after 10 failures ({type(exc).__name__})")

    log = [json.loads(l) for l in (TMP / "actions.jsonl").read_text().splitlines()]
    print(f"action log: {len(log)} entries, e.g. {[(e['tool'], e['allowed']) for e in log[:6]]}")
    print("\n=== AIROX Connect WSL check: " + ("PASS" if all(results.values()) else "FAIL"), f"{sum(results.values())}/{len(results)}")
    svc.stop_lan()
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
