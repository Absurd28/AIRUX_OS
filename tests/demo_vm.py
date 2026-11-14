#!/usr/bin/env python3
"""Dry run of docs/DEMO.md in QEMU before the real-hardware demo (run as root).

Boots the ISO's live session, then speaks the five demo commands into AIRA with `aira-voice --file`
as the live user. The commands are synthesized on the host with a voice that is not AIRA's. Each
outcome is checked inside the VM. Command 4 is confirmed by typing "yes", as the user would. At the
end the VM's audio output is transcribed, to show that AIRA spoke.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import phase8_check as p8  # noqa: E402

vm, D, sh = p8.vm, p8.D, p8.sh
USER, HOME = "airux", "/home/airux"
DEMO = [
    "How much free disk space do I have in my home folder?",
    "Create a file called demo.txt in my home folder that says hello from AIRA.",
    "Remind me in 2 minutes to stretch.",
    "Delete demo.txt in my home folder.",
    "What's the weather in Pune right now?",
]


def say(i: int, text: str, k) -> str:
    import numpy as np
    s, sr = k.create(text, voice="am_michael", lang="en-us")
    local = D / f"demo{i}.wav"
    with wave.open(str(local), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(s, -1, 1) * 32767).astype("<i2").tobytes())
    vm.put(D, local, f"/tmp/demo{i}.wav")
    sh(f"chmod 644 /tmp/demo{i}.wav")
    return f"/tmp/demo{i}.wav"


def main() -> int:
    vm.stop(D)
    time.sleep(2)
    for f in ("demo.qcow2", "OVMF_VARS.fd", "audio.wav"):
        (D / f).unlink(missing_ok=True)
    vm.start(D, p8.ISO, D / "demo.qcow2", "cd")
    print(f"live system booted; guest agent after {p8.wait_agent():.0f}s", flush=True)
    base = p8.common_checks("live session")
    uid = ""
    for _ in range(60):
        uid = sh(f"id -u {USER}")["out"].strip()
        if uid and sh(f"test -S /run/user/{uid}/bus && echo y")["out"].strip() == "y":
            break
        time.sleep(5)
    env = (f"HOME={HOME} XDG_RUNTIME_DIR=/run/user/{uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus "
           "DISPLAY=:0")
    print("reminder daemon:", sh(f"runuser -u {USER} -- env {env} systemctl --user is-active aira-reminderd")["out"].strip())
    from kokoro_onnx import Kokoro
    k = Kokoro(str(p8.WORK / "models/kokoro-v1.0.onnx"), str(p8.WORK / "models/voices-v1.0.bin"))
    results = []
    t_reminder = 0.0
    for i, cmd in enumerate(DEMO, 1):
        wav = say(i, cmd, k)
        t0 = time.monotonic()
        out = vm.gexec(D, ["/bin/sh", "-c", f"runuser -u {USER} -- env {env} aira-voice --file {wav}"],
                       timeout=900, input_data=b"yes\n" if i == 4 else None)
        secs = time.monotonic() - t0
        text = out["out"]
        print(f"\n--- demo {i} ({secs:.0f}s): {cmd}\n{text.strip()}", flush=True)
        if i == 1:
            df = sh(f"df -BG --output=avail {HOME} | tail -1")["out"].strip()
            nums = re.findall(r"(\d+(?:\.\d+)?)\s*GiB", text)
            ok = "AIRA:" in text and bool(nums) and "not spoken aloud" not in text
            note = f"AIRA said {nums} GiB free; df says {df}"
        elif i == 2:
            content = sh(f"cat {HOME}/demo.txt 2>&1")["out"].strip()
            ok = "hello from aira" in content.lower() and "Verified" in text
            note = f"~/demo.txt: {content!r}"
        elif i == 3:
            ok = "stretch" in text.lower() and "not spoken aloud" not in text
            t_reminder = time.monotonic()
            note = "reminder set; checked after the due time below"
        elif i == 4:
            gone = sh(f"test -e {HOME}/demo.txt && echo present || echo gone")["out"].strip()
            asked = "wants to run desktop.delete_path" in text
            ok = asked and gone == "gone"
            note = f"confirmation asked: {asked}; file {gone}"
        else:
            ok = "not sure" in text.lower() and not re.search(r"\d+\s*°|degrees|sunny|rain(y|ing)?\b", text, re.I)
            note = "abstained" if ok else "did not abstain"
        print(f"=> {'PASS' if ok else 'FAIL'}: {note}", flush=True)
        results.append((i, ok, round(secs), note))
    # the reminder: due 2 minutes after demo 3; the daemon checks every 30 s
    wait = max(0.0, 170 - (time.monotonic() - t_reminder))
    print(f"\nwaiting {wait:.0f}s for the reminder to fire", flush=True)
    time.sleep(wait)
    log = sh(f"journalctl -b --no-pager -o cat _UID={uid} | grep -i -m3 'stretch'")["out"].strip()
    vm.shot(D, D / "demo-reminder.png")
    fired = "stretch" in log.lower()
    print(f"reminder daemon log: {log or '(nothing)'}")
    results[2] = (3, results[2][1] and fired, results[2][2], f"reminder fired: {fired}")
    pid = (D / "qemu.pid").read_text().strip()   # QEMU deletes its pid file when it exits
    try:
        sh("systemctl poweroff", timeout=20)
    except (OSError, TimeoutError, RuntimeError):
        pass
    for _ in range(60):   # the WAV file is complete only once QEMU has exited
        if not Path(f"/proc/{pid}").exists():
            break
        time.sleep(2)
    secs = p8.trim_speech(D / "audio.wav", D / "demo-said.wav") if (D / "audio.wav").exists() else 0.0
    heard = ""
    if secs:
        b = p8.WORK / "bin-whisper"
        res = subprocess.run([str(b / "whisper-cli"), "-m", str(p8.WORK / "models/ggml-base.en.bin"), "-f",
                              str(D / "demo-said.wav"), "-nt", "-np"], capture_output=True, text=True,
                             env=dict(os.environ, LD_LIBRARY_PATH=str(b)))
        heard = " ".join(res.stdout.split())
    print(f"\nVM audio: {secs:.0f}s of sound; whisper heard: {heard[:1500]!r}")
    print("\n=== demo dry run:", f"{sum(r[1] for r in results)}/5 passed", "(live system ok)" if base["ok"] else
          "(LIVE SYSTEM CHECKS FAILED)")
    for i, ok, s, note in results:
        print(f"  {i}. {'PASS' if ok else 'FAIL'} [{s}s] {DEMO[i - 1]} -- {note}")
    return 0 if all(r[1] for r in results) and base["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
