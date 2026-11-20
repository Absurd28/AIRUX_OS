#!/usr/bin/env python3
"""Phagos check on the live ISO in QEMU (run as root, like desktop_check). Pass requires:

  service     phagosd runs; real-time protection on; the immune model came learned with the image;
              decoys are in place; the user's agent runs
  eicar       the EICAR test file saved to ~/Downloads is quarantined at once and recorded
  exec_block  a program on the local blocklist is refused when started (Operation not permitted)
  ransomware  a harmless simulator (it only overwrites test files it made itself) is paused within
              seconds, before it gets through its files, and is stopped when the user chooses
  anomaly     an unknown, packed-looking program in /tmp is flagged and not allowed to run (its
              Negative Selection score is reported; alone it would only be noted)
  clean       a scan of /usr/bin, /usr/sbin, /opt/aira and Chromium finds nothing (no false alarms)
  restore     restoring from quarantine (administrator) puts the file back, and it is then allowed
  aira        AIRA answers "is my computer protected?" and "scan my Downloads" from Phagos's tool
  app         the Phagos Security window opens
Screenshots: docs/screenshots/phagos-*.png
"""
from __future__ import annotations

import json
import re
import shlex
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import desktop_check as dc  # noqa: E402

vm, D, sh, p8 = dc.vm, dc.D, dc.sh, dc.p8
USER, HOME = dc.USER, f"/home/{dc.USER}"
EICAR = r"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
SHOTS = REPO / "docs" / "screenshots"

# harmless: makes its own folder of plain text files, then overwrites each with random bytes and
# renames it, like ransomware would; it never touches anything else
SIM = """import os, sys, time
d = os.path.expanduser("~/Documents/phagos-sim")
os.makedirs(d, exist_ok=True)
names = []
for i in range(400):
    p = os.path.join(d, f"report-{i:03d}.txt")
    with open(p, "w") as f:
        f.write(("quarterly figures, region %d\\n" % i) * 400)
    names.append(p)
print("made", len(names), flush=True)
time.sleep(6)                                  # let the creation writes age out
for p in names:
    with open(p, "r+b") as f:
        f.write(os.urandom(os.path.getsize(p)))
    os.rename(p, p + ".locked")
    time.sleep(0.02)
print("done", flush=True)
"""

ASK = """import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.connect("/run/phagos/phagos.sock")
s.sendall((json.dumps({"method": sys.argv[1], "params": json.loads(sys.argv[2])}) + "\\n").encode())
print(s.makefile().readline().strip())"""


def api(env: str, method: str, as_root: bool = False, **params) -> dict:
    cmd = f"python3 /tmp/phagos-ask.py {method} {shlex.quote(json.dumps(params))}"
    out = (sh(cmd) if as_root else dc.as_user(env, cmd))["out"].strip().splitlines()
    return json.loads(out[-1]) if out else {"ok": False, "error": "no output"}


def wait(fn, timeout: float, step: float = 1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def main() -> int:
    vm.stop(D)
    time.sleep(2)
    for f in ("desk.qcow2", "OVMF_VARS.fd"):
        (D / f).unlink(missing_ok=True)
    vm.start(D, p8.ISO, D / "desk.qcow2", "cd")
    print(f"live system booted; guest agent after {p8.wait_agent():.0f}s", flush=True)
    env = wait(dc.session_env, 300, 5) or ""
    if not env:
        print("=== Phagos check: FAIL (no Plasma session)")
        return 1
    dc.wait_window(env, r"^AIRA$", 180)
    sh("cat > /tmp/phagos-ask.py <<'PY'\n" + ASK + "\nPY\nchmod 644 /tmp/phagos-ask.py")
    r = {}

    # --- service
    wait(lambda: sh("systemctl is-active phagosd")["out"].strip() == "active", 60)
    st = wait(lambda: (lambda s: s if s.get("ok") and s["ransomware_shield"]["decoys"] else None)(api(env, "status")), 90, 3)
    agent = sh(f"pgrep -u {USER} -f phagos.agent")["out"].strip()
    print(f"status: {json.dumps(st)[:600]}\nagent: {agent or None}")
    r["service"] = bool(st) and st["realtime"] and st["engines"]["self_model"] == "ready" \
        and st["engines"]["detectors"] > 0 and bool(agent)
    vm.shot(D, SHOTS / "phagos-1-desktop.png")

    # --- EICAR into Downloads
    dc.as_user(env, f"printf '%s' {shlex.quote(EICAR)} > {HOME}/Downloads/eicar.com")
    gone = wait(lambda: sh(f"test -e {HOME}/Downloads/eicar.com || echo gone")["out"].strip() == "gone", 15, 0.5)
    hist = api(env, "history", limit=10).get("events", [])
    hit = [e for e in hist if e.get("threat") == "EICAR-Test-File"]
    print(f"EICAR: removed={bool(gone)}; history: {hit[:1]}")
    r["eicar"] = bool(gone) and bool(hit) and "quarantined" in (hit[0].get("action") or "")
    time.sleep(3)
    vm.shot(D, SHOTS / "phagos-2-eicar-notification.png")

    # --- exec of a blocklisted program
    dc.as_user(env, "cp /usr/bin/true /tmp/phagos-blocked && printf 'phagos-test-marker' >> /tmp/phagos-blocked "
                    "&& chmod +x /tmp/phagos-blocked")
    before = dc.as_user(env, "/tmp/phagos-blocked; echo rc=$?")["out"].strip()
    digest = sh("sha256sum /tmp/phagos-blocked | cut -d' ' -f1")["out"].strip()
    sh(f"echo '{digest} Test.Blocklisted' > /etc/phagos/rules.d/test.sha256 && systemctl restart phagosd")
    wait(lambda: api(env, "status").get("ok"), 60, 2)
    after = dc.as_user(env, "/tmp/phagos-blocked; echo rc=$?")
    print(f"blocklisted program: before rule '{before}'; after rule '{after['out'].strip()} {after['err'].strip()}'")
    r["exec_block"] = before.endswith("rc=0") and "rc=0" not in after["out"] and \
        ("not permitted" in (after["out"] + after["err"]).lower() or "rc=126" in after["out"])

    # --- ransomware-like behaviour
    sh("cat > /tmp/phagos-sim.py <<'PY'\n" + SIM + "\nPY\n" f"install -o {USER} -g {USER} -m 644 /tmp/phagos-sim.py {HOME}/sim.py")
    dc.as_user(env, f"setsid python3 {HOME}/sim.py > /tmp/phagos-sim.log 2>&1 &")
    wait(lambda: "made" in sh("cat /tmp/phagos-sim.log")["out"], 60, 1)
    t0 = time.monotonic()
    paused = wait(lambda: [p for p in (api(env, "status").get("paused") or []) if "sim.py" in (p.get("script") or "")], 60, 1)
    secs = time.monotonic() - t0
    locked = int(sh(f"ls {HOME}/Documents/phagos-sim | grep -c locked")["out"].strip() or 0)
    pid = paused[0]["pid"] if paused else None
    state = sh(f"awk '{{print $3}}' /proc/{pid}/stat")["out"].strip() if pid else ""
    print(f"simulator paused after {secs:.0f}s: {paused[:1]}; state {state}; files encrypted {locked}/400")
    time.sleep(3)
    vm.shot(D, SHOTS / "phagos-3-ransomware-paused.png")
    decided = api(env, "decide", event_id=paused[0]["event_id"], choice="kill") if paused else {}
    dead = wait(lambda: sh(f"test -d /proc/{pid} || echo dead")["out"].strip() == "dead", 10, 0.5) if pid else None
    print(f"decision: {decided}; process gone: {bool(dead)}")
    r["ransomware"] = bool(paused) and state == "T" and locked < 400 and bool(dead)

    # --- an unknown packed-looking program in /tmp
    dc.as_user(env, "cp /usr/bin/true /tmp/odd-tool && head -c 300000 /dev/urandom >> /tmp/odd-tool && chmod +x /tmp/odd-tool")
    time.sleep(3)
    run = dc.as_user(env, "/tmp/odd-tool; echo rc=$?")
    nsa = sh("cd /usr/lib/phagos && python3 -c \"import json; from phagos import features as F; "
             "from phagos.engines.nsa import NegativeSelection as N; "
             "n = N.from_json(json.load(open('/var/lib/phagos/nsa.json'))); i = F.read('/tmp/odd-tool'); "
             "print(round(n.score(i.vector)[0], 3), round(n.score(F.read('/usr/bin/true').vector)[0], 3))\"")["out"].split()
    sus = [e for e in api(env, "history", limit=20).get("events", []) if e.get("path") == "/tmp/odd-tool"]
    print(f"odd tool: run '{run['out'].strip()} {run['err'].strip()}'; NSA score (odd, /usr/bin/true) {nsa}; events {sus[:1]}")
    r["anomaly"] = bool(sus) and sus[0].get("level") == "suspicious" and "rc=0" not in run["out"] \
        and len(nsa) == 2 and float(nsa[1]) == 0      # a system program is never non-self

    # --- no false alarms on the system's own programs
    clean = {}
    for path in ("/usr/bin", "/usr/sbin", "/opt/aira", "/usr/lib/chromium"):
        api(env, "scan", as_root=True, kind="custom", path=path)
        job = wait(lambda: (lambda s: s if s.get("done") else None)(api(env, "scan_status", as_root=True).get("scan") or {}),
                   1800, 3)
        clean[path] = (job or {}).get("scanned"), [f["path"] for f in (job or {}).get("found", [])]
    print(f"clean scans (files, found): {clean}")
    r["clean"] = all(n and not found for n, found in clean.values())

    # --- restore from quarantine (as an administrator)
    items = api(env, "quarantine_list", as_root=True).get("items", [])
    eicar = [i for i in items if i["threat"] == "EICAR-Test-File"]
    res = api(env, "quarantine_restore", as_root=True, id=eicar[0]["id"]) if eicar else {}
    back = sh(f"test -f {HOME}/Downloads/eicar.com && echo back")["out"].strip()
    # written again by the user: allowed now (same contents), so it stays
    dc.as_user(env, f"cp {HOME}/Downloads/eicar.com /tmp/e && cp /tmp/e {HOME}/Downloads/eicar.com")
    time.sleep(3)
    still = sh(f"test -f {HOME}/Downloads/eicar.com && echo still")["out"].strip()
    user_try = api(env, "quarantine_list")        # a user sees only their own items
    print(f"restore: {res.get('ok')}, back={back}, still there after a rewrite={still}; user sees {len(user_try.get('items', []))}")
    r["restore"] = bool(res.get("ok")) and back == "back" and still == "still"

    # --- AIRA
    tok = dc.as_user(env, "cat $HOME/.local/state/aira/shell.token")["out"].split()
    replies = {}
    for q in ("Is my computer protected from viruses?", "Scan my Downloads folder for viruses."):
        if len(tok) == 2:
            sh("cat > /tmp/aira-ask.py <<'PY'\n" + ASK_AIRA + "\nPY\nchmod 644 /tmp/aira-ask.py")
            out = dc.as_user(env, f"python3 /tmp/aira-ask.py {tok[0]} {tok[1]} {shlex.quote(q)}", timeout=400)["out"]
            replies[q] = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        print(f"AIRA: {q} -> {json.dumps(replies.get(q))[:400]}")
    used = [all(c["tool"] == "security" for c in rep.get("calls", [])) and rep.get("calls") for rep in replies.values()]
    r["aira"] = len(replies) == 2 and all(used)
    time.sleep(2)
    vm.shot(D, SHOTS / "phagos-4-aira.png")

    # --- the app
    dc.as_user(env, "setsid phagos-security >/dev/null 2>&1 &")
    win = dc.wait_window(env, r"^Phagos Security$", 90)
    time.sleep(5)
    vm.shot(D, SHOTS / "phagos-5-app.png")
    print(f"app window: {win}")
    r["app"] = bool(win)

    print("\n=== Phagos check:", "PASS" if all(r.values()) else "FAIL", r)
    return 0 if all(r.values()) else 1


ASK_AIRA = """import json, sys, time, urllib.request
port, token, text = sys.argv[1], sys.argv[2], sys.argv[3]
base = f"http://127.0.0.1:{port}"
hdr = {"X-AIRA-Token": token, "Content-Type": "application/json"}
t0 = time.time()
urllib.request.urlopen(urllib.request.Request(base + "/command", json.dumps({"text": text}).encode(), hdr), timeout=10)
for line in urllib.request.urlopen(f"{base}/events?token={token}", timeout=360):
    if not line.startswith(b"data: "):
        continue
    ev = json.loads(line[6:])
    if ev.get("event") == "aira" and ev.get("ts", 0) >= t0:
        print(json.dumps({"text": ev.get("text"), "calls": ev.get("calls", [])}))
        break"""


if __name__ == "__main__":
    sys.exit(main())
