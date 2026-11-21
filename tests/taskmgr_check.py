#!/usr/bin/env python3
"""AIRUX Task Manager check in QEMU. Run as root in WSL.

  taskmgr_check.py --boot   boot the ISO first;  taskmgr_check.py   use the running VM

Pass requires every line:
  shortcut   Ctrl+Shift+Esc is registered and opens the Task Manager window
  processes  its process list holds the user's processes (as `ps` sees them) and groups apps
  confirm    ending a test process asks first: "no" leaves it running, "yes" ends it (read back)
  log        both decisions are in the action log
Screenshot: $AIRUX_WORK/build/vm/taskmgr-window.png
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import desktop_check as dc  # noqa: E402

vm, D, sh, p8 = dc.vm, dc.D, dc.sh, dc.p8

# runs inside the VM as the user (Debian's python3): talks to the Task Manager's own local API
PROBE = r'''
import json, sys, threading, time, urllib.request, subprocess
from pathlib import Path
port, token = (Path.home() / ".local/state/airux-taskmgr/ui.token").read_text().split()
base = f"http://127.0.0.1:{port}"
def api(m, **p):
    r = urllib.request.Request(base + "/api", data=json.dumps({"method": m, "params": p}).encode(),
                               headers={"X-Token": token, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=30).read())
out = {}
time.sleep(2)
pr = api("processes")
pids = {p["pid"] for g in pr["groups"] for p in g["procs"]}
ps = {int(x) for x in subprocess.run(["ps", "-u", sys.argv[1], "-o", "pid="], capture_output=True, text=True).stdout.split()}
out["listed"] = len(ps & pids); out["ps"] = len(ps)
out["apps"] = [g["name"] for g in pr["groups"] if g["kind"] == "app"]
events = []
def listen():
    for line in urllib.request.urlopen(f"{base}/events?token={token}", timeout=600):
        if line.startswith(b"data: "):
            events.append(json.loads(line[6:]))
threading.Thread(target=listen, daemon=True).start()
time.sleep(1)
for answer in (False, True):
    victim = subprocess.Popen(["sleep", "5151"])
    time.sleep(2.5)
    p = next(p for g in api("processes")["groups"] for p in g["procs"] if p["pid"] == victim.pid)
    api("run", tool="end_task", args={"pid": victim.pid, "start": p["start"]})
    for _ in range(100):
        ask = next((e for e in events if e.get("event") == "confirm" and not e.get("seen")), None)
        if ask: break
        time.sleep(0.1)
    ask["seen"] = True
    api("confirm", id=ask["id"], yes=answer)
    for _ in range(100):
        res = next((e for e in events if e.get("event") == "result" and not e.get("seen")), None)
        if res: break
        time.sleep(0.1)
    res["seen"] = True
    time.sleep(0.5)
    out["yes" if answer else "no"] = {"asked": ask["summary"], "risks": ask["risks"], "allowed": res["allowed"],
                                      "detail": (res.get("data") or {}).get("detail"), "running": victim.poll() is None}
    if victim.poll() is None and answer is False:
        victim.kill()
print(json.dumps(out))
'''


def main() -> int:
    if "--boot" in sys.argv:
        vm.stop(D)
        time.sleep(2)
        for f in ("tm.qcow2", "OVMF_VARS.fd", "audio.wav"):
            (D / f).unlink(missing_ok=True)
        vm.start(D, p8.ISO, D / "tm.qcow2", "cd")
        print(f"live system booted; guest agent after {p8.wait_agent():.0f}s", flush=True)
    env = ""
    for _ in range(90):
        env = dc.session_env()
        if env:
            break
        time.sleep(5)
    if not env:
        print("=== Task Manager check: FAIL (no Plasma session)")
        return 1
    r: dict[str, bool] = {}
    print(sh("dpkg-query -W -f='${Package} ${Version} ${Status}\\n' airux-taskmgr")["out"].strip())
    comp = dc.as_user(env, "qdbus6 --literal org.kde.kglobalaccel /component/airux_taskmgr_desktop "
                           "org.kde.kglobalaccel.Component.allShortcutInfos")["out"]
    registered = "117440512" in comp    # Qt key code of Ctrl+Shift+Esc (0x04000000 | 0x02000000 | 0x01000000)
    dc.close_windows(env)
    vm.key(D, "ctrl-shift-esc")
    win = dc.wait_window(env, r"^Task Manager$", 60)
    print(f"Ctrl+Shift+Esc registered: {registered}; window: {win}")
    r["shortcut"] = registered and bool(win)
    time.sleep(4)
    vm.shot(D, D / "taskmgr-window.png")

    sh("cat > /tmp/tmprobe.py <<'PY'\n" + PROBE + "\nPY\nchmod 644 /tmp/tmprobe.py")
    raw = dc.as_user(env, "python3 /tmp/tmprobe.py airux", 180)["out"].strip().splitlines()
    try:
        out = json.loads(raw[-1])
    except (ValueError, IndexError):
        print(f"probe output: {raw}")
        print("=== Task Manager check: FAIL (probe)")
        return 1
    print(json.dumps(out, indent=1))
    r["processes"] = out["listed"] >= 0.9 * out["ps"] and len(out["apps"]) >= 1
    no, yes = out["no"], out["yes"]
    r["confirm"] = (no["asked"].startswith("End sleep") and not no["allowed"] and no["running"]
                    and yes["allowed"] and not yes["running"] and "has ended" in (yes["detail"] or ""))
    log = dc.as_user(env, "cat ~/.local/state/airux-taskmgr/actions.jsonl 2>/dev/null | tail -5")["out"]
    in_vault = "end_task" not in log
    if in_vault:      # the action log is in AIRA's encrypted vault when it could be opened: ask the log there
        log = dc.as_user(env, "/opt/aira/venv/bin/python - <<'PY'\nimport sys; sys.path.insert(0, '/opt/aira/app')\n"
                              "from aira.config import Config\nfrom aira.memory.keys import get_key\nfrom aira.memory.vault import Vault\n"
                              "v = Vault(Config().vault_path, get_key()[0])\n"
                              "print([r[0] for r in v.db.execute(\"SELECT text FROM items WHERE kind='action' ORDER BY id DESC LIMIT 6\")])\nPY", 60)["out"]
    r["log"] = log.count("end_task") >= 2 and "Task Manager" in log
    print(f"action log: {log.strip()[-600:]}")
    for k, v in r.items():
        print(f"{'PASS' if v else 'FAIL'} {k}")
    ok = all(r.values())
    print(f"\n=== Task Manager check: {'PASS' if ok else 'FAIL'} {sum(r.values())}/{len(r)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
