#!/usr/bin/env python3
"""A screenshot tour of AIRUX working, on the live ISO in QEMU (run as root, like desktop_check).

Each scene is real: AIRA answers real commands with the real model, AIROX Code asks her about real
code, NEOX loads a real page. Images go to docs/screenshots/tour-NN-name.png. A scene that does not
come up is reported, and the tour carries on.
"""
from __future__ import annotations

import shlex
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import desktop_check as dc  # noqa: E402

vm, D, sh, p8 = dc.vm, dc.D, dc.sh, dc.p8
OUT = REPO / "docs" / "screenshots"
USER, HOME = dc.USER, f"/home/{dc.USER}"

# asks AIRA through her shell, answers "Yes" to browser confirmations (as the user would click it),
# and prints her reply when it comes
ASK_PY = """import json, sys, time, urllib.request
port, token, text, limit = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
base = f"http://127.0.0.1:{port}"
hdr = {"X-AIRA-Token": token, "Content-Type": "application/json"}
def post(path, body):
    return urllib.request.urlopen(urllib.request.Request(base + path, json.dumps(body).encode(), hdr), timeout=10).read()
t0 = time.time()
post("/command", {"text": text})
for line in urllib.request.urlopen(f"{base}/events?token={token}", timeout=limit):
    if time.time() - t0 > limit:
        break
    if not line.startswith(b"data: "):
        continue
    ev = json.loads(line[6:])
    if ev.get("ts", t0) < t0 - 1:
        continue
    if ev.get("event") == "confirm" and ev.get("tool") in ("browser", "web_search"):
        post("/confirm", {"id": ev["id"], "yes": True})
    if ev.get("event") == "aira":
        print(ev.get("text", ""))
        break"""


class Tour:
    def __init__(self):
        self.env = ""
        self.n = 0
        self.done: list[tuple[str, bool, str]] = []

    def user(self, cmd: str, timeout: float = 60) -> dict:
        return dc.as_user(self.env, cmd, timeout)

    def shot(self, name: str, ok: bool = True, note: str = "") -> None:
        self.n += 1
        path = OUT / f"tour-{self.n:02d}-{name}.png"
        vm.shot(D, path)
        self.done.append((path.name, ok, note))
        print(f"  [{'ok' if ok else 'MISSING'}] {path.name} {note}", flush=True)

    def ask(self, text: str, limit: float = 300) -> str:
        tok = self.user("cat $HOME/.local/state/aira/shell.token")["out"].split()
        if len(tok) != 2:
            return ""
        sh("cat > /tmp/ask.py <<'PY'\n" + ASK_PY + "\nPY\nchmod 644 /tmp/ask.py")
        return self.user(f"python3 /tmp/ask.py {tok[0]} {tok[1]} {shlex.quote(text)} {limit}",
                         timeout=limit + 30)["out"].strip()

    def app(self, cmd: str, pattern: str, wait: float = 120):
        self.user(f"setsid {cmd} >/dev/null 2>&1 &")   # session env and PATH, as from the Start menu
        return dc.wait_window(self.env, pattern, wait)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    vm.stop(D)
    time.sleep(2)
    for f in ("desk.qcow2", "OVMF_VARS.fd"):
        (D / f).unlink(missing_ok=True)
    vm.start(D, p8.ISO, D / "desk.qcow2", "cd")
    print(f"live system booted; guest agent after {p8.wait_agent():.0f}s", flush=True)
    t = Tour()
    for _ in range(60):
        t.env = dc.session_env()
        if t.env:
            break
        time.sleep(5)
    if not t.env:
        print("no Plasma session")
        return 1
    aira = dc.wait_window(t.env, r"^AIRA$", 180)
    for _ in range(60):   # the model is still loading for a while after login
        if '"ok"' in sh("curl -s -m 3 http://127.0.0.1:8080/health")["out"]:
            break
        time.sleep(5)
    time.sleep(15)

    # 1. the desktop, with AIRA docked; 2. the Start menu
    t.shot("desktop", bool(aira))
    vm.key(D, "meta_l")
    time.sleep(4)
    t.shot("start-menu")
    vm.key(D, "esc")
    time.sleep(2)

    # 3. AIRA answering a question about this computer
    reply = t.ask("How much free disk space do I have in my home folder?")
    print(f"AIRA: {reply[:200]}")
    time.sleep(3)
    t.shot("aira-answers", bool(reply), reply[:80])

    # 4. AIROX Code: AIRA explains the code that is selected
    sh(f"""install -d -o {USER} -g {USER} {HOME}/Projects/calc
cat > {HOME}/Projects/calc/calc.py <<'PY'
def add(a, b):
    return a + b


def sub(a, b):
    return a + b


def mean(values):
    return sum(values) / len(values)
PY
chown {USER}:{USER} {HOME}/Projects/calc/calc.py""")
    code = t.app(f"airox-code --new-window {HOME}/Projects/calc {HOME}/Projects/calc/calc.py", r"calc\.py", 150)
    if code:
        time.sleep(8)
        vm.key(D, "ctrl-alt-i")       # AIRA's chat
        time.sleep(4)
        vm.click(D, 150, 703)         # the chat box
        vm.type_text(D, "Why does sub in calc.py give the wrong result? Cite the line." + chr(10))
        time.sleep(150)
    t.shot("airox-code-aira", bool(code))
    dc.close_windows(t.env)

    # 5. NEOX on a real page, with AIRA's side panel open (Ctrl+Shift+Y) summarizing it
    neox = t.app("neox https://www.debian.org/", r"(?i)debian", 120)
    if neox:
        time.sleep(6)
        vm.click(D, 640, 450)         # focus the page
        vm.key(D, "ctrl-shift-y")     # NEOX: open AIRA's side panel
        time.sleep(6)
        vm.click(D, 846, 299)         # "Summarize": AIRA answers from the page, quoting it
        time.sleep(100)
    t.shot("neox-aira-panel", bool(neox))

    # 6. AIRA browsing in NEOX for you
    reply = t.ask("Open the browser and search for the Debian 13 release notes")
    found = dc.wait_window(t.env, r"(?i)release notes", 120)
    time.sleep(4)
    t.shot("aira-browses-neox", bool(found), reply[:80])
    dc.close_windows(t.env)

    # 7-9. games: chess against Stockfish, Sudoku, AIRUX Snake (a game in progress)
    chess = t.app("knights", r"Knights", 90)
    if chess:
        for _ in range(3):
            time.sleep(4)
            if sh("pgrep -x stockfish")["out"].strip():
                break
            vm.key(D, "ret")
        time.sleep(4)
    t.shot("chess", bool(chess))
    sh(f"pkill -u {USER} -x knights")   # closing asks to save the game in progress
    dc.close_windows(t.env)
    sudoku = t.app("ksudoku", r"KSudoku", 90)
    if sudoku:
        time.sleep(4)
        vm.click(D, sudoku[1] + 454, sudoku[2] + 260)   # "Generate A Puzzle"
        time.sleep(6)
    t.shot("sudoku", bool(sudoku))
    sh(f"pkill -u {USER} -x ksudoku")
    dc.close_windows(t.env)
    snake = t.app("airux-snake", r"^AIRUX Snake$", 90)
    if snake:
        time.sleep(3)
        vm.click(D, snake[1] + snake[3] // 2, snake[2] + snake[4] // 2)   # give the game the keyboard
        time.sleep(0.5)
        vm.key(D, "1")                # speed 1: about five cells a second
        vm.key(D, "ret")
        # a safe loop on the 24x18 board from the middle: up, left, down, right
        for wait, k in [(0.8, "up"), (1.0, "left"), (1.5, "down"), (1.2, "right")]:
            time.sleep(wait)
            vm.key(D, k)
        time.sleep(1.0)
    t.shot("snake", bool(snake))
    dc.close_windows(t.env)

    # 10. Files; 11. About this system (the AIRUX logo); 12. the installer
    files = t.app("dolphin", r"Dolphin", 90)
    time.sleep(3)
    t.shot("files", bool(files))
    dc.close_windows(t.env)
    about = t.app("kinfocenter", r"(?i)about this system|info center", 90)
    time.sleep(4)
    t.shot("about", bool(about))
    dc.close_windows(t.env)
    sh(f"env {t.env} setsid calamares >/dev/null 2>&1 &")
    inst = dc.wait_window(t.env, r"(?i)install|calamares|airux", 90)
    time.sleep(5)
    t.shot("installer", bool(inst))

    vm.stop(D)
    print("\n=== tour:", sum(ok for _, ok, _ in t.done), "of", len(t.done), "scenes came up")
    for name, ok, note in t.done:
        print(f"  {'ok     ' if ok else 'MISSING'} {name} {note}")
    return 0 if all(ok for _, ok, _ in t.done) else 1


if __name__ == "__main__":
    sys.exit(main())
