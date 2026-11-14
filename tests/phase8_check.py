#!/usr/bin/env python3
"""Phase 8 check: the ISO boots in QEMU, installs to disk, and AIRA answers by voice.

Stages (run as root; the install in between is driven through scripts/qemu_ctl.py):
  phase8_check.py live        boot the ISO; check AIRUX branding, AIRA packages and services
  (install with Calamares: erase disk, user from tests/phase8_vm.toml, log in automatically)
  phase8_check.py installed   boot the installed disk (no ISO); check the installed system, then
                              speak a command into AIRA (aira-voice --file, as the logged-in user),
                              capture the VM's audio output, and transcribe it on the host.
"""
from __future__ import annotations

import array
import json
import os
import subprocess
import sys
import time
import tomllib
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))

import qemu_ctl as vm  # noqa: E402

WORK = Path(os.environ.get("AIRUX_WORK", "/srv/airux-work"))
D = WORK / "build" / "vm"
ISO = WORK / "build" / "iso" / "airux-0.1-amd64.hybrid.iso"
DISK = D / "airux.qcow2"
ACCOUNT = tomllib.loads((REPO / "tests" / "phase8_vm.toml").read_text())
SAID = "How much free disk space do I have in my home folder?"


def wait_agent(timeout: float = 600) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if vm.ping(D):
            return time.monotonic() - t0
        time.sleep(5)
    raise TimeoutError("the guest agent never answered (VM did not boot?)")


def sh(cmd: str, timeout: float = 300) -> dict:
    return vm.gexec(D, ["/bin/sh", "-c", cmd], timeout=timeout)


def wait_active(units: list[str], timeout: float = 300) -> dict:
    end = time.monotonic() + timeout
    st = {}
    while time.monotonic() < end:
        st = {u: sh(f"systemctl is-active {u}")["out"].strip() for u in units}
        if all(v == "active" for v in st.values()):
            break
        time.sleep(5)
    return st


def common_checks(label: str) -> dict:
    r = {}
    r["os_release"] = sh("grep -E '^(PRETTY_NAME|NAME)=' /etc/os-release")["out"].strip()
    r["packages"] = sh("dpkg-query -W -f='${Package} ${Version} ${Status}\\n' aira aira-models-base")["out"].strip()
    r["plymouth"] = sh("plymouth-set-default-theme 2>/dev/null || readlink /etc/alternatives/default.plymouth")[
        "out"].strip()
    r["services"] = wait_active(["aira-llm", "aira-embed", "aira-stt"])
    r["recommendation"] = sh("cat /var/lib/aira/recommendation.json 2>/dev/null | head -c 400")["out"]
    r["llm_log"] = sh("journalctl -u aira-llm -b --no-pager -o cat | grep -m1 'aira-llm:'")["out"].strip()
    for _ in range(60):  # models load from the medium: give them up to 5 minutes
        r["health"] = sh("curl -s -m 5 127.0.0.1:8080/health; echo; curl -s -m 5 127.0.0.1:8081/health")["out"].strip()
        if r["health"].count('"ok"') == 2:
            break
        time.sleep(5)
    print(f"== {label}")
    for k, v in r.items():
        print(f"{k}: {v}")
    ok = ("AIRUX" in r["os_release"] and r["packages"].count("install ok installed") == 2
          and all(v == "active" for v in r["services"].values()) and r["health"].count('"ok"') == 2)
    return {"ok": ok, **r}


def stage_live() -> int:
    if not ISO.exists():
        print(f"no ISO at {ISO}")
        return 1
    vm.stop(D)
    time.sleep(2)
    for f in ("airux.qcow2", "OVMF_VARS.fd", "audio.wav"):
        (D / f).unlink(missing_ok=True)
    vm.start(D, ISO, DISK, "cd")
    print(f"live system booted; guest agent after {wait_agent():.0f}s")
    r = common_checks("live session")
    for _ in range(36):
        r["user"] = sh("id airux")["out"].strip()
        if r["user"]:
            break
        time.sleep(5)
    print(f"live user: {r['user']}")
    vm.shot(D, D / "live-desktop.png")
    session = sh("loginctl list-sessions --no-legend | grep -w airux")["out"].strip()
    print(f"live session: {session or '(none)'}")
    ok = r["ok"] and "aira" in r["user"] and bool(session)
    print("PASS (live)" if ok else "FAIL (live)")
    return 0 if ok else 1


def trim_speech(src: Path, dst: Path) -> float:
    """Cut the VM's audio capture down to the stretch with sound, as 16 kHz mono for whisper."""
    with wave.open(str(src)) as w:
        ch, rate, width = w.getnchannels(), w.getframerate(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    a = array.array("h", raw)
    mono = a[::ch]
    win = rate // 10
    loud = [i for i in range(0, len(mono) - win, win) if max(abs(x) for x in mono[i:i + win]) > 1500]
    if not loud:
        return 0.0
    start, end = max(0, loud[0] - win * 3), min(len(mono), loud[-1] + win * 5)
    seg = mono[start:end]
    step = rate / 16000
    out = array.array("h", (seg[int(i * step)] for i in range(int(len(seg) / step))))
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(out.tobytes())
    return len(out) / 16000


def stage_installed() -> int:
    vm.stop(D)
    time.sleep(3)
    (D / "audio.wav").unlink(missing_ok=True)
    vm.start(D, None, DISK, "disk")
    print(f"installed system booted from disk; guest agent after {wait_agent():.0f}s")
    r = common_checks("installed system")
    user = ACCOUNT["login"]
    r["user"] = sh(f"id {user}")["out"].strip()
    uid = sh(f"id -u {user}")["out"].strip()
    sess = sh(f"loginctl list-sessions --no-legend | grep -w {user} | head -1")["out"].strip()
    print(f"user: {r['user']}\nsession: {sess or '(none yet)'}")
    for _ in range(36):  # wait for the graphical session (autologin) and its audio
        if sh(f"test -S /run/user/{uid}/pulse/native && echo yes")["out"].strip() == "yes":
            break
        time.sleep(5)
    env = f"XDG_RUNTIME_DIR=/run/user/{uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus"
    for _ in range(12):  # PipeWire must be up with a real output, or AIRA cannot be heard
        r["sinks"] = sh(f"runuser -u {user} -- env {env} pactl list short sinks")["out"].strip()
        if "alsa_output" in r["sinks"]:
            break
        time.sleep(5)
    print(f"audio sinks: {r['sinks'] or '(none)'}")
    # the spoken command, synthesized on the host with a voice that is not AIRA's
    from kokoro_onnx import Kokoro
    import numpy as np
    k = Kokoro(str(WORK / "models/kokoro-v1.0.onnx"), str(WORK / "models/voices-v1.0.bin"))
    s, sr = k.create(SAID, voice="am_michael", lang="en-us")
    cmd_wav = D / "said.wav"
    with wave.open(str(cmd_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(s, -1, 1) * 32767).astype("<i2").tobytes())
    vm.put(D, cmd_wav, "/tmp/said.wav")
    sh("chmod 644 /tmp/said.wav")
    t0 = time.monotonic()
    out = sh(f"runuser -u {user} -- env HOME=/home/{user} {env} aira-voice --file /tmp/said.wav", timeout=900)
    took = time.monotonic() - t0
    print(f"aira-voice ({took:.0f}s, exit {out['exitcode']}):\n{out['out']}{out['err'][-1500:]}")
    time.sleep(3)
    vm.shot(D, D / "installed-desktop.png")
    pid = (D / "qemu.pid").read_text().strip()   # QEMU deletes its pid file when it exits
    try:  # the guest agent dies with the guest, so the reply to this call may never come
        sh("systemctl poweroff", timeout=20)
    except (OSError, TimeoutError, RuntimeError):
        pass
    for _ in range(60):
        if not Path(f"/proc/{pid}").exists():
            break
        time.sleep(2)
    secs = trim_speech(D / "audio.wav", D / "aira-said.wav") if (D / "audio.wav").exists() else 0.0
    heard = ""
    if secs:
        b = WORK / "bin-whisper"
        res = subprocess.run([str(b / "whisper-cli"), "-m", str(WORK / "models/ggml-base.en.bin"), "-f",
                              str(D / "aira-said.wav"), "-nt", "-np"], capture_output=True, text=True,
                             env=dict(os.environ, LD_LIBRARY_PATH=str(b)))
        heard = " ".join(res.stdout.split())
    print(f"VM audio output: {secs:.1f}s of sound; whisper heard: {heard!r}")
    replied = "AIRA:" in out["out"] and "you:" in out["out"]
    spoke = secs > 1.0 and any(w in heard.lower() for w in ("free", "space", "home", "gib", "gigabyte"))
    replied = replied and "not spoken aloud" not in out["out"]
    ok = r["ok"] and str(r["user"]).find("aira") >= 0 and "alsa_output" in r["sinks"] and replied and spoke
    print("=== phase 8:", "PASS" if ok else "FAIL",
          {"installed_system": r["ok"], "audio_sink": "alsa_output" in r["sinks"], "user_in_aira_group": "aira" in r["user"], "aira_replied": replied,
           "aira_spoke": spoke})
    return 0 if ok else 1


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit(stage_live() if stage == "live" else stage_installed() if stage == "installed" else 2)
