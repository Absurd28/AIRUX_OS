"""AIRA shell server: http://127.0.0.1:8765, stdlib only.

GET  /               the shell page (token embedded)
GET  /events         server-sent events: transcript, agents, confirmations
POST /command        {"text": ...}            run a typed command
POST /confirm        {"id": ..., "yes": bool}  answer a pending confirmation
POST /ptt/start      start push-to-talk recording;  POST /ptt/stop  stop and run it
POST /ptt/toggle     start or stop (for an Xfce keyboard shortcut: aira-ptt)

Security: only 127.0.0.1; every POST needs the per-session token (X-AIRA-Token header), and the
Host header must be exactly 127.0.0.1:<port>. A web page open in the AIRA browser therefore cannot
send commands or approve confirmations (no token, custom header forces a CORS preflight that this
server never answers, and DNS rebinding fails the Host check). Unanswered confirmations are denied
after CONFIRM_TIMEOUT seconds.
"""
from __future__ import annotations

import json
import os
import queue
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC = Path(__file__).parent / "static"
CONFIRM_TIMEOUT = 120.0


def tags_for(reply: str) -> list[str]:
    """Verified / unverified tags shown next to a reply."""
    tags = []
    if "Verified:" in reply:
        tags.append("verified")
    if "Not verified" in reply or "[unsupported]" in reply or reply.startswith("Unverified") \
            or "\nUnverified" in reply:
        tags.append("unverified")
    if "Not done:" in reply:
        tags.append("not done")
    if reply.startswith("I'm not sure") or "I couldn't verify" in reply:
        tags.append("not sure")
    if "Sources:" in reply:
        tags.append("cited")
    return tags


class Shell:
    def __init__(self, port: int = 8765, token: str | None = None, token_file: Path | None = None):
        self.port = port
        self.token = token or secrets.token_urlsafe(24)
        self.token_file = token_file
        self.orch = None
        self.voice = None
        self.recorder = None
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.pending: dict[str, dict] = {}
        self.history: list[dict] = []
        self.busy = threading.Lock()
        self.httpd: ThreadingHTTPServer | None = None

    # ---- events
    def publish(self, ev: dict) -> None:
        ev = {"ts": time.time(), **ev}
        with self._lock:
            if ev.get("event") in ("you", "aira"):
                self.history.append(ev)
                del self.history[:-200]
            subs = list(self._subs)
        for q in subs:
            q.put(ev)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
            for ev in self.history:
                q.put(ev)
            for p in self.pending.values():
                q.put(p["event"])
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    # ---- confirmations (the broker's confirm callback)
    def confirm(self, agent: str, tool: str, args: dict, risks) -> bool:
        cid = uuid.uuid4().hex[:12]
        ev = {"event": "confirm", "id": cid, "agent": agent, "tool": tool, "args": args,
              "risks": sorted(r.value for r in risks), "timeout": CONFIRM_TIMEOUT}
        done = threading.Event()
        with self._lock:
            self.pending[cid] = {"event": ev, "done": done, "answer": False}
        self.publish(ev)
        answered = done.wait(CONFIRM_TIMEOUT)
        with self._lock:
            answer = self.pending.pop(cid)["answer"] if answered else False
            self.pending.pop(cid, None)
        self.publish({"event": "confirm_done", "id": cid, "yes": answer,
                      "why": "answered" if answered else "timed out (denied)"})
        return answer

    def answer(self, cid: str, yes: bool) -> bool:
        with self._lock:
            p = self.pending.get(cid)
            if p is None:
                return False
            p["answer"] = bool(yes)
            p["done"].set()
        return True

    # ---- running commands
    def attach(self, orch, voice_session=None, recorder=None) -> None:
        self.orch, self.voice, self.recorder = orch, voice_session, recorder
        orch.listeners.append(self.publish)

    def _finish(self, turn) -> None:
        self.publish({"event": "aira", "text": turn.reply, "kind": turn.kind, "tags": tags_for(turn.reply),
                      "calls": [{"agent": r.agent, "tool": s.tool, "args": s.args, "verified": s.verified}
                                for r in turn.runs for s in r.tool_calls]})

    def run_text(self, text: str) -> None:
        def work():
            with self.busy:
                self.publish({"event": "you", "text": text, "via": "typed"})
                self.publish({"event": "thinking"})
                try:
                    self._finish(self.orch.handle(text))
                except Exception as exc:
                    self.publish({"event": "aira", "text": f"I couldn't verify that: {exc}", "tags": ["not sure"]})
                self.publish({"event": "idle"})
        threading.Thread(target=work, daemon=True).start()

    def ptt(self, action: str) -> dict:
        if self.recorder is None or self.voice is None:
            return {"ok": False, "error": "voice is not available"}
        if action == "toggle":
            action = "stop" if self.recorder.proc is not None else "start"
        if action == "start":
            self.recorder.start()
            self.publish({"event": "listening"})
            return {"ok": True, "state": "listening"}
        self.run_audio(self.recorder.stop())
        return {"ok": True, "state": "thinking"}

    def run_audio(self, wav: Path) -> threading.Thread:
        """What happens after push-to-talk stops: transcribe, run, show and speak the reply."""
        def work():
            with self.busy:
                self.publish({"event": "thinking"})
                vt = self.voice.handle_audio(wav)
                self.publish({"event": "you", "text": vt.transcript or "(nothing heard)", "via": "voice"})
                if vt.turn is not None:
                    self._finish(vt.turn)
                else:
                    self.publish({"event": "aira", "text": vt.spoken, "tags": []})
                self.publish({"event": "spoken", "ok": bool(vt.speech.get("spoken")), "voice": vt.speech})
                self.publish({"event": "idle"})
        th = threading.Thread(target=work, daemon=True)
        th.start()
        return th

    # ---- HTTP
    def handler(self):
        shell = self

        class H(BaseHTTPRequestHandler):
            server_version = "aira-shell"

            def log_message(self, *a):
                pass

            def _host_ok(self) -> bool:
                return self.headers.get("Host") in (f"127.0.0.1:{shell.port}", f"localhost:{shell.port}")

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                 "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, b'{"error":"bad host"}')
                if self.path == "/":
                    page = (STATIC / "index.html").read_text().replace("__AIRA_TOKEN__", shell.token)
                    return self._send(200, page.encode(), "text/html; charset=utf-8")
                if self.path.startswith("/events"):
                    if not secrets.compare_digest(self.path.partition("token=")[2], shell.token):
                        return self._send(403, b'{"error":"token"}')
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    q = shell.subscribe()
                    try:
                        while True:
                            try:
                                ev = q.get(timeout=15)
                                self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
                            except queue.Empty:
                                self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        shell.unsubscribe(q)
                    return
                self._send(404, b'{"error":"not found"}')

            def do_POST(self):
                if not self._host_ok():
                    return self._send(403, b'{"error":"bad host"}')
                if not secrets.compare_digest(self.headers.get("X-AIRA-Token", ""), shell.token):
                    return self._send(403, b'{"error":"token"}')
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                except ValueError:
                    return self._send(400, b'{"error":"bad json"}')
                if self.path == "/command":
                    text = str(body.get("text", "")).strip()
                    if not text:
                        return self._send(400, b'{"error":"empty"}')
                    if shell.busy.locked():
                        return self._send(409, b'{"error":"busy"}')
                    shell.run_text(text)
                    return self._send(202, b'{"ok":true}')
                if self.path == "/confirm":
                    ok = shell.answer(str(body.get("id", "")), bool(body.get("yes")))
                    return self._send(200 if ok else 404, json.dumps({"ok": ok}).encode())
                m = re.fullmatch(r"/ptt/(start|stop|toggle)", self.path)
                if m:
                    try:
                        return self._send(200, json.dumps(shell.ptt(m.group(1))).encode())
                    except Exception as exc:
                        return self._send(500, json.dumps({"ok": False, "error": str(exc)}).encode())
                self._send(404, b'{"error":"not found"}')

            def do_OPTIONS(self):  # never allow cross-origin preflight
                self._send(403, b'{"error":"no cross-origin requests"}')
        return H

    def serve(self) -> ThreadingHTTPServer:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), self.handler())
        self.port = self.httpd.server_port
        if self.token_file:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(f"{self.port} {self.token}\n")
        threading.Thread(target=self.httpd.serve_forever, name="aira-shell-http", daemon=True).start()
        return self.httpd

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
