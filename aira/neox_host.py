"""NEOX native messaging host (org.airux.neox): Chromium starts it for the NEOX extension only.

It relays between the extension (stdin/stdout, Chrome's native-messaging framing) and:
  - AIRA's agents, through the private Unix socket served by aira.neox.NeoxBridge (AIRA's commands
    run in NEOX; the results go back);
  - AIRA's model, for side-panel questions about the current page (aira.neox_qa: answer only from
    the page, quotes checked in code);
  - the AIRA shell, for "Ask AIRA" commands (posted to /command with the shell's token; AIRA's
    replies and confirmation requests are relayed back).
Nothing here listens on the network.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import neox_qa
from .neox import read_native, socket_path, write_native

MODEL = os.environ.get("AIRA_SERVER", "http://127.0.0.1:8080")


class Host:
    def __init__(self, stdin, stdout, sock_path: Path | None = None, model_url: str = MODEL,
                 shell_token_file: Path | None = None):
        self.stdin, self.stdout = stdin, stdout
        self.sock_path = sock_path or socket_path()
        self.model_url = model_url
        self.token_file = shell_token_file or (Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
                                               / "aira" / "shell.token")
        self._out = threading.Lock()
        self._aira: socket.socket | None = None
        self.running = True

    # ---------------------------------------------------------- to the extension
    def to_ext(self, msg: dict) -> None:
        with self._out:
            write_native(self.stdout, msg)

    # ---------------------------------------------------------- AIRA's agents (Unix socket)
    def _aira_loop(self) -> None:
        while self.running:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(self.sock_path))
            except OSError:
                time.sleep(2)
                continue
            self._aira = s
            try:
                for line in s.makefile("rb"):
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("type") == "command":
                        self.to_ext(msg)
            except OSError:
                pass
            self._aira = None
            time.sleep(1)

    def to_aira(self, msg: dict) -> None:
        s = self._aira
        if s is not None:
            try:
                s.sendall(json.dumps(msg).encode() + b"\n")
            except OSError:
                pass

    # ---------------------------------------------------------- side panel: questions about a page
    def ask_page(self, msg: dict) -> None:
        page = msg.get("page") or {}
        msgs = neox_qa.messages(msg.get("question", ""), page)
        try:
            text = self._chat(msgs)
            check = neox_qa.check(text, page.get("text", ""))
            if check["unverified"]:
                # like AIRA's reply checker: one retry, naming the quotes that are not on the page
                msgs += [{"role": "assistant", "content": text}, {"role": "user", "content": neox_qa.retry_prompt(check)}]
                text = self._chat(msgs)
                check = neox_qa.check(text, page.get("text", ""))
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
            self.to_ext({"type": "error", "id": msg.get("id"), "text": f"AIRA's model isn't reachable ({exc})."})
            return
        self.to_ext({"type": "answer", "id": msg.get("id"), "text": text, **check})

    def _chat(self, msgs: list[dict]) -> str:
        body = json.dumps({"messages": msgs, "temperature": 0.1, "max_tokens": 600, "stream": False}).encode()
        req = urllib.request.Request(self.model_url + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"].strip()

    # ---------------------------------------------------------- side panel: commands for AIRA's agents
    def aira_command(self, msg: dict) -> None:
        mid = msg.get("id")
        try:
            port, token = self.token_file.read_text().split()
        except (OSError, ValueError):
            self.to_ext({"type": "error", "id": mid, "text": "AIRA isn't running. Open AIRA first."})
            return
        base = f"http://127.0.0.1:{port}"
        events = urllib.request.urlopen(f"{base}/events?token={token}", timeout=600)
        req = urllib.request.Request(base + "/command", data=json.dumps({"text": msg.get("text", "")}).encode(),
                                     headers={"Content-Type": "application/json", "X-AIRA-Token": token})
        try:
            urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as exc:
            events.close()
            why = "AIRA is busy with another request" if exc.code == 409 else f"AIRA refused ({exc.code})"
            self.to_ext({"type": "error", "id": mid, "text": why + "."})
            return
        for raw in events:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            if ev.get("event") == "confirm":
                self.to_ext({"type": "aira_event", "id": mid, "event": "confirm",
                             "text": f"{ev.get('tool', '')} {json.dumps(ev.get('args', {}))[:160]}"})
            elif ev.get("event") == "aira":
                self.to_ext({"type": "aira_event", "id": mid, "event": "aira", "text": ev.get("text", "")})
                break
        events.close()

    # ---------------------------------------------------------- main loop
    def run(self) -> None:
        threading.Thread(target=self._aira_loop, name="neox-host-aira", daemon=True).start()
        while self.running:
            msg = read_native(self.stdin)
            if msg is None:          # the browser closed the extension's port
                self.running = False
                break
            t = msg.get("type")
            if t == "result":
                self.to_aira(msg)
            elif t == "ask_page":
                threading.Thread(target=self.ask_page, args=(msg,), daemon=True).start()
            elif t == "aira_command":
                threading.Thread(target=self.aira_command, args=(msg,), daemon=True).start()


def main() -> int:
    Host(sys.stdin.buffer, sys.stdout.buffer).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
