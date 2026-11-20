#!/usr/bin/env python3
"""AIRUX Drivers and firmware on the live ISO in QEMU (run as root). Pass requires:

  firmware    every firmware package of the image is installed, and the kernel logged no firmware
              load failures
  diagnosis   airux-drivers (as the user) finds this VM's network, graphics and sound working and
              nothing to fix; the boot-time collector has written its file
  helper      the root helper refuses packages outside its allowlist, and installs one inside it
              (firmware-linux-free, already there: apt runs end to end)
  nvidia      apt can install NVIDIA's driver from the image's sources (a simulated install:
              nvidia-driver + nvidia-open-kernel-dkms resolve; QEMU has no NVIDIA GPU)
  printing    the print service answers
  report      a hardware report is saved in the user's home folder
  app         the AIRUX Drivers window opens
  aira        AIRA answers "is all my hardware working?" with the hardware tool
"""
from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import desktop_check as dc  # noqa: E402
from phagos_check import ASK_AIRA  # noqa: E402

vm, D, sh, p8 = dc.vm, dc.D, dc.sh, dc.p8
USER, HOME = dc.USER, f"/home/{dc.USER}"
SHOTS = REPO / "docs" / "screenshots"
LIST = REPO / "image/live-build/config/package-lists/airux.list.chroot"


def main() -> int:
    firmware_pkgs = [l.strip() for l in LIST.read_text().splitlines()
                     if l.strip().startswith(("firmware-", "bluez-firmware", "intel-microcode", "amd64-microcode"))]
    vm.stop(D)
    time.sleep(2)
    for f in ("desk.qcow2", "OVMF_VARS.fd"):
        (D / f).unlink(missing_ok=True)
    vm.start(D, p8.ISO, D / "desk.qcow2", "cd")
    print(f"live system booted; guest agent after {p8.wait_agent():.0f}s", flush=True)
    env = ""
    for _ in range(60):
        env = dc.session_env()
        if env:
            break
        time.sleep(5)
    dc.wait_window(env, r"^AIRA$", 180)
    time.sleep(20)
    r = {}

    st = sh("dpkg-query -W -f='${Package} ${Status}\\n' " + " ".join(firmware_pkgs + ["airux-drivers", "cups", "fwupd"]))["out"]
    missing = [p for p in firmware_pkgs + ["airux-drivers", "cups", "fwupd"] if f"{p} install ok installed" not in st]
    fails = sh("journalctl -k -b --no-pager -o cat | grep -E 'Direct firmware load|firmware: failed to load' | head -5")["out"].strip()
    print(f"firmware packages: {len(firmware_pkgs)}; missing: {missing}; kernel firmware failures: {fails or 'none'}")
    r["firmware"] = not missing and not fails

    out = dc.as_user(env, "airux-drivers status --json")["out"].strip().splitlines()
    status = json.loads(out[-1]) if out else {}
    rows = {f["category"]: (f["status"], f["device"], f["detail"]) for f in status.get("findings", [])}
    collected = sh("cat /run/airux-drivers/kernel.json")["out"].strip()
    print(f"diagnosis: {json.dumps(status.get('summary'))}")
    for k, v in rows.items():
        print(f"  {k:10} {v}")
    print(f"collector: {collected}")
    r["diagnosis"] = bool(status) and status["summary"]["all_working"] and rows.get("ethernet", ("",))[0] == "working" \
        and rows.get("graphics", ("",))[0] == "working" and collected.startswith('{"firmware_failures"')

    bad = sh("/usr/lib/airux-drivers/airux-drivers-helper install bash; echo rc=$?")["out"]
    good = sh("/usr/lib/airux-drivers/airux-drivers-helper install firmware-linux-free; echo rc=$?", timeout=900)["out"]
    bad_res = [l for l in bad.splitlines() if l.startswith("RESULT ")]
    good_res = [l for l in good.splitlines() if l.startswith("RESULT ")]
    print(f"helper refuses bash: {bad_res}\nhelper installs firmware-linux-free: {good_res} (tail: {good.strip().splitlines()[-3:]})")
    r["helper"] = bool(bad_res) and '"ok": false' in bad_res[0] and "not allowed" in bad_res[0] \
        and bool(good_res) and '"ok": true' in good_res[0]

    sim = sh("grep -h '^deb ' /etc/apt/sources.list /etc/apt/sources.list.d/*.list 2>/dev/null | head -3; "
             "apt-get -s install nvidia-driver nvidia-open-kernel-dkms \"linux-headers-$(uname -r)\" 2>&1 | "
             "grep -E '^Inst (nvidia-driver|nvidia-open-kernel-dkms|linux-headers)|^E:' | head -6", timeout=600)["out"]
    print(f"NVIDIA (simulated install):\n{sim}")
    r["nvidia"] = "Inst nvidia-driver " in sim and "Inst nvidia-open-kernel-dkms " in sim and "E:" not in sim

    lp = dc.as_user(env, "lpstat -r")["out"].strip()
    print(f"printing: {lp}")
    r["printing"] = "scheduler is running" in lp

    rep = dc.as_user(env, "airux-drivers report")["out"].strip()
    head = sh(f"head -3 {shlex.quote(rep)}")["out"] if rep.startswith(HOME) else ""
    print(f"report: {rep}\n{head}")
    r["report"] = "AIRUX hardware report" in head

    dc.as_user(env, "setsid airux-drivers-app >/dev/null 2>&1 &")
    win = dc.wait_window(env, r"^AIRUX Drivers$", 90)
    time.sleep(5)
    vm.shot(D, SHOTS / "drivers-app.png")
    print(f"app window: {win}")
    r["app"] = bool(win)

    tok = dc.as_user(env, "cat $HOME/.local/state/aira/shell.token")["out"].split()
    rep_aira = {}
    if len(tok) == 2:
        sh("cat > /tmp/aira-ask.py <<'PY'\n" + ASK_AIRA + "\nPY\nchmod 644 /tmp/aira-ask.py")
        out = dc.as_user(env, f"python3 /tmp/aira-ask.py {tok[0]} {tok[1]} "
                              f"{shlex.quote('Is all my hardware working? Check my drivers.')}", timeout=400)["out"]
        rep_aira = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    print(f"AIRA: {json.dumps(rep_aira)[:500]}")
    r["aira"] = any(c["tool"] == "hardware" for c in rep_aira.get("calls", []))

    print("\n=== AIRUX Drivers check:", "PASS" if all(r.values()) else "FAIL", r)
    return 0 if all(r.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
