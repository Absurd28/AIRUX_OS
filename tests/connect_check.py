#!/usr/bin/env python3
"""AIROX Connect end to end: AIRUX in QEMU + the Android app in the emulator. Run as root in WSL.

  connect_check.py --boot      boot the ISO (airox-connect and airux-taskmgr installed in it)
  connect_check.py             use the VM that is already running (tests/desktop_check.py helpers)

The app's instrumented test (../AIROX_Connect, org.airux.connect.E2E) drives the phone; this
script plays the person at the PC (allows the pairing, answers KDE's "Remote control" and "Screen
Sharing" windows once) and checks every result on the PC. Ports 8766/8768 are forwarded from QEMU;
the emulator reaches them at 10.0.2.2 through Windows. Pass needs every line:
  1 pair       pairing link from the PC; the PC allowed it; the app connected
  2 aira       a typed and a spoken command (whisper on the PC) get AIRA's answers on the phone
  3 tasks      the phone lists the test processes; "no" leaves one running, "yes" ends the other
  4 files      phone->PC upload and PC->phone download, SHA-256 equal both ways
  5 clipboard  both ways; a link from the phone opens in NEOX on the PC
  6 input      the touchpad moves the PC's pointer; a media key changes the volume
  7 screen     a glance image; the second screen (a new virtual output) shows frames with a measured
               delay, and a tap lands on it
  8 alerts     a 1-minute reminder asked by the phone arrives as a phone notification; AIRA's own
               confirmation answered on the phone runs the delete
  9 refuse     no token, a wrong token and a wrong certificate pin are refused (a non-LAN address
               and a reused code: tests/connect_wsl_check.py)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import desktop_check as dc  # noqa: E402

vm, D, sh, p8 = dc.vm, dc.D, dc.sh, dc.p8
_GUEST = threading.RLock()


def _locked(fn):
    def call(*a, **k):
        with _GUEST:
            return fn(*a, **k)
    return call


# QEMU's guest-agent and QMP channels take one request at a time; steps run in their own threads
for _name in ("gexec", "put", "get", "key", "click", "shot", "type_text"):
    setattr(vm, _name, _locked(getattr(vm, _name)))
WIN = "/mnt/c/Users/Aryan's_WorkSpace"
ADB = os.environ.get("ADB", f"{WIN}/AppData/Local/Android/Sdk/platform-tools/adb.exe")
APP = Path(os.environ.get("AIROX_CONNECT", REPO.parent / "AIROX_Connect")) / "app" / "build" / "outputs" / "apk"
VOICE = Path(os.environ.get("AIRUX_WORK", "/srv/airux-work")) / "build" / "vm" / "demo1.wav"
SHOTS = D
PIN = "2468"
results: dict[str, list] = {}
facts: dict[str, str] = {}
env = ""


def check(item: str, ok: bool, detail: str) -> None:
    results.setdefault(item, []).append(bool(ok))
    print(f"  {'ok ' if ok else 'FAIL'} {item}: {detail}", flush=True)


def adb(*args, timeout=120, check_rc=False) -> str:
    for attempt in range(3):          # WSL's interop (a Windows program from WSL) can time out under load
        r = subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout)
        if "UtilAcceptVsock" not in r.stderr:
            break
        time.sleep(5)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def winpath(p: Path) -> str:
    return subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True).stdout.strip()


def user(cmd: str, timeout: float = 60) -> str:
    return dc.as_user(env, cmd, timeout)["out"]


def local(method: str, **params) -> dict:
    body = json.dumps({"method": method, "params": params})
    out = user("read port token < ~/.local/state/airox-connect/ui.token; curl -s -m 150 -H 'Content-Type: application/json' "
               "-H \"X-Token: $token\" --data " + shlex.quote(body) + " http://127.0.0.1:$port/api", 200)
    return json.loads(out or "{}")


def cursor() -> tuple[int, int]:
    dc.kwin_script(env, 'print("CUR|" + workspace.cursorPos.x + "|" + workspace.cursorPos.y);')
    time.sleep(0.5)
    line = user("journalctl --user -b --no-pager -o cat | grep 'CUR|' | tail -1")
    m = re.search(r"CUR\|(\d+)\|(\d+)", line)
    return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)


def window_list() -> list[tuple[str, int, int, int, int]]:
    """(caption, x, y, width, height) of every window, dialogs included (dc.windows lists normal ones)."""
    token = f"AXCAP{int(time.time() * 1000)}"
    with _GUEST:
        dc.kwin_script(env, 'workspace.windowList().forEach(function (w) { var g = w.frameGeometry; print("' + token +
                       '|" + w.caption + "|" + g.x + "|" + g.y + "|" + g.width + "|" + g.height); });')
        out = user(f"journalctl --user -b --no-pager -o cat | grep '{token}|'")
    wins = []
    for l in out.splitlines():
        if f"{token}|" in l:
            f = l.split(f"{token}|", 1)[1].rsplit("|", 4)
            if len(f) == 5:
                wins.append((f[0], *(round(float(v)) for v in f[1:])))
    return wins


def captions() -> list[str]:
    return [w[0] for w in window_list()]


def answer_dialog(caption: str, dx: int | None = None, dy: int | None = None) -> bool:
    """Bring a KDE dialog to the front (KWin) and press its default button (Enter), or click at an offset
    from its top-left corner, as the person at the PC would."""
    w = next((w for w in window_list() if w[0] == caption), None)
    if w is None:
        return False
    with _GUEST:
        dc.kwin_script(env, 'workspace.windowList().forEach(function (w) { if (w.caption == ' + json.dumps(caption) +
                       ') workspace.activeWindow = w; });')
        time.sleep(0.5)
        if dx is None:
            vm.key(D, "ret")
        else:
            vm.shot(D, D / "connect-pc-dialog.png")
            vm.click(D, w[1] + dx, w[2] + dy)
    return True


def phone_shot(name: str) -> None:
    data = subprocess.run([ADB, "exec-out", "screencap", "-p"], capture_output=True, timeout=30).stdout
    if data.startswith(b"\x89PNG"):
        (SHOTS / f"connect-phone-{name}.png").write_bytes(data)


def volume() -> float:
    m = re.search(r"Volume:\s*([0-9.]+)", user("wpctl get-volume @DEFAULT_AUDIO_SINK@"))
    return float(m.group(1)) if m else -1.0


# ------------------------------------------------------------------ the PC side, reacting to the phone's steps
def react(step: str) -> None:
    if step == "pair_waiting":
        for _ in range(240):
            reqs = local("state").get("pairing", {}).get("requests", [])
            if reqs:
                local("approve", id=reqs[0]["id"], yes=True)
                check("pair", True, f"the PC allowed '{reqs[0]['name']}' ({reqs[0]['model']}, from {reqs[0]['ip']})")
                return
            time.sleep(0.5)
        check("pair", False, "no pairing request reached the PC")
    elif step == "paired":
        time.sleep(3)
        phone_shot("home")
    elif step == "tasks_done":
        a = user("pgrep -f '^(/usr/bin/)?sleep 4242$' >/dev/null && echo alive || echo gone")
        b = user("pgrep -f '^(/usr/bin/)?sleep 4243$' >/dev/null && echo alive || echo gone")
        check("tasks", "alive" in a and "gone" in b, f"'no' -> sleep 4242 {a.strip()}; 'yes' -> sleep 4243 {b.strip()}")
    elif step == "uploaded":
        got = user("sha256sum ~/Downloads/'AIROX Connect'/phone-photo.jpg 2>/dev/null | cut -c1-64").strip()
        facts["pc_upload_sha"] = got
    elif step == "link_opened":
        ok = False
        for _ in range(60):
            if any("Example Domain" in c for c in captions()):
                ok = True
                break
            time.sleep(1)
        clip = user("wl-paste --no-newline 2>/dev/null").strip()
        check("clipboard", clip == "from the phone 1234", f"PC clipboard now '{clip}'")
        check("clipboard", ok, "NEOX shows 'Example Domain' from the phone's link" if ok else f"no NEOX window with the page: {captions()}")
    elif step == "input_start":
        # KDE's "Remote control requested" (a dialog, maybe not listed as a normal window): the person at
        # the PC presses Share (Enter); KDE remembers it
        for i in range(90):
            if local("state").get("input") == "ready":
                break
            if answer_dialog("Remote control requested"):
                print("  (PC: allowed remote control)", flush=True)
                time.sleep(2)
            time.sleep(1)
    elif step == "input_ready":
        facts["cursor_before"] = json.dumps(cursor())
    elif step == "pointer_moved":
        time.sleep(1)
        b, a = json.loads(facts.get("cursor_before", "[-1,-1]")), cursor()
        check("input", a[0] - b[0] > 100 and a[1] - b[1] > 50, f"pointer {tuple(b)} -> {a}")
    elif step == "glance":
        # Spectacle captures the glance on AIRUX; the ScreenCast fallback would ask KDE which screen, but
        # the harness must not answer that chooser here: the second screen's chooser comes right after
        pass
    elif step == "second_screen":
        # KDE's "Screen Sharing" chooser, the first time: the person at the PC picks "New Virtual Output"
        for i in range(60):
            if "Virtual-virtual" in user("kscreen-doctor -o 2>/dev/null"):
                break
            if answer_dialog("Screen Sharing", 142, 155):    # the "New Virtual Output" tile
                print("  (PC: chose New Virtual Output)", flush=True)
                time.sleep(3)
            time.sleep(1)
        time.sleep(45)
        phone_shot("second-screen-45s")
        print("  [phone stream log] " + " | ".join(l.split(": ", 1)[-1] for l in
              adb("logcat", "-d", "-s", "AIROXStream:I").splitlines() if "AIROXStream" in l)[-600:], flush=True)
    elif step == "second_screen_wait":
        pass
    elif step == "stream_tap":
        time.sleep(1)
        phone_shot("second-screen")
        outs = user("kscreen-doctor -o 2>/dev/null | sed 's/\\x1b\\[[0-9;]*m//g' | grep -E 'Output|Geometry'")
        facts["outputs"] = " ".join(outs.split())
    elif step == "stream_tapped":
        x, y = cursor()
        m = re.search(r"Virtual-virtual\S*\s+\S+\s+\S+\s+Geometry: (\d+),(\d+) (\d+)x(\d+)", facts.get("outputs", ""))
        if m:
            vx, vy, vw, vh = map(int, m.groups())
            inside = vx <= x < vx + vw and vy <= y < vy + vh
            check("screen", inside, f"virtual output at {vx},{vy} {vw}x{vh}; the tap put the pointer at {x},{y}")
        else:
            check("screen", False, f"no virtual output while streaming: {facts.get('outputs')}")
    elif step == "aira_confirmed":
        gone = user("test -e ~/Documents/e2e-delete-me.txt && echo there || echo gone").strip()
        check("alerts", gone == "gone", f"after 'yes' on the phone, ~/Documents/e2e-delete-me.txt is {gone}")


def on_result(key: str, val: str) -> None:
    facts[key] = val
    if key == "paired":
        check("pair", val not in ("", "null"), f"the app is connected to '{val}'")
    elif key in ("no_token", "wrong_token"):
        check("refuse", val == "401", f"{key.replace('_', ' ')} -> {val}")
    elif key == "wrong_pin":
        check("refuse", val.startswith("refused"), f"wrong certificate pin -> {val}")
    elif key == "aira_text":
        check("aira", len(val) > 5, f"typed -> \"{val[:90]}\"")
    elif key == "voice_reply":
        check("aira", len(val) > 5, f"spoken (heard \"{facts.get('voice_heard', '')}\") -> \"{val[:80]}\"")
    elif key == "download_sha256":
        check("files", val == facts.get("e2e_sha"), f"PC->phone e2e.jpg SHA-256 {val[:12]}... (PC {facts.get('e2e_sha', '')[:12]}...)")
    elif key == "pc_clipboard":
        check("clipboard", val == "from the PC 5678", f"phone read the PC clipboard: '{val}'")
    elif key == "volume":
        after = volume()
        check("input", 0 <= after < float(facts.get("volume_before", "2")), f"volume {facts.get('volume_before')} -> {after:.2f}")
    elif key == "glance":
        facts["glance"] = val
        check("screen", "x" in val, f"glance {val}")
    elif key == "latency_ms":
        check("screen", val not in ("null", "") and int(val) >= 0, f"second screen: frames shown, PC->phone delay {val} ms "
              f"(emulator + QEMU, software decode)")
    elif key == "notification":
        check("alerts", val not in ("null", ""), f"phone notification: {val}")


def main() -> int:
    global env
    boot = "--boot" in sys.argv
    if boot:
        vm.stop(D)
        time.sleep(2)
        for f in ("connect.qcow2", "OVMF_VARS.fd", "audio.wav"):
            (D / f).unlink(missing_ok=True)
        # 6.5 GB: the live system keeps its writes in RAM, and the 2B model, Plasma, AIRA's voice and
        # the second screen's encoder all run together (5 GB ran out of memory after ~40 minutes)
        vm.start(D, p8.ISO, D / "connect.qcow2", "cd", mem=int(os.environ.get("AIRUX_VM_MEM", "6656")))
        print(f"live system booted; guest agent after {p8.wait_agent():.0f}s", flush=True)
    for _ in range(90):
        env = dc.session_env()
        if env:
            break
        time.sleep(5)
    if not env:
        print("=== AIROX Connect check: FAIL (no Plasma session)")
        return 1
    for rule in ("tcp::8766-:8766", "tcp::8768-:8768"):
        vm.qmp(D).cmd("human-monitor-command", **{"command-line": f"hostfwd_add n0 {rule}"})
    if boot:
        time.sleep(30)                    # AIRA, the shell and airox-connectd start at login
    deb = os.environ.get("AIRUX_CONNECT_DEB")            # a newer airox-connect than the ISO's, for a quick retest
    if deb:
        vm.put(D, Path(deb), "/tmp/airox-connect.deb")
        sh("dpkg -i /tmp/airox-connect.deb >/dev/null")
        user(r"systemctl --user restart 'app-airox\x2dconnectd@autostart.service'")
        time.sleep(5)
    pk = sh("dpkg-query -W -f='${Package} ${Version}\\n' airox-connect airux-taskmgr")["out"].strip()
    print(f"packages: {pk}")
    running = user("pgrep -fa airox_connect.server | head -1")
    if not running:
        user("systemd-run --user --collect --unit=airox-connectd-check airox-connectd")
        time.sleep(5)

    print("== prepare the PC", flush=True)
    user("systemctl --user stop sleeper-a sleeper-b 2>/dev/null; systemd-run --user --collect --unit=sleeper-a sleep 4242; "
         "systemd-run --user --collect --unit=sleeper-b sleep 4243; mkdir -p ~/Pictures ~/Documents; "
         "head -c 2000000 /dev/urandom > ~/Pictures/e2e.jpg; echo 'delete me' > ~/Documents/e2e-delete-me.txt; "
         "rm -f ~/Downloads/'AIROX Connect'/phone-photo*.jpg; printf 'from the PC 5678' | wl-copy >/dev/null 2>&1")
    facts["e2e_sha"] = user("sha256sum ~/Pictures/e2e.jpg | cut -c1-64").strip()
    user("wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.6")
    facts["volume_before"] = str(volume())
    st = local("enable", on=True)
    for d in st.get("devices", []):                     # a clean start: forget phones from earlier runs
        local("revoke", id=d["id"])
    uri = local("pair_start")["uri"]
    uri = re.sub(r"h=[^&]+", "h=10.0.2.2", uri)
    print(f"pairing link: {uri[:70]}...", flush=True)

    print("== the phone", flush=True)
    if "device" not in adb("get-state"):
        print("=== AIROX Connect check: FAIL (no emulator or phone on adb)")
        return 1
    adb("install", "-r", "-t", winpath(APP / "debug" / "app-debug.apk"), timeout=300, check_rc=True)
    adb("install", "-r", "-t", winpath(APP / "androidTest" / "debug" / "app-debug-androidTest.apk"), timeout=300, check_rc=True)
    for perm in ("POST_NOTIFICATIONS", "RECORD_AUDIO", "CAMERA"):
        adb("shell", "pm", "grant", "org.airux.connect", f"android.permission.{perm}")
    dev_voice = "/sdcard/Android/data/org.airux.connect/files/say.wav"
    adb("shell", "mkdir", "-p", "/sdcard/Android/data/org.airux.connect/files")
    adb("push", winpath(VOICE), dev_voice)
    # a screen lock, so the phone can approve "strong" actions (deleting) as a person would; the screen
    # stays on so it never locks during the run; removed again at the end
    adb("shell", "locksettings", "set-pin", PIN)
    adb("shell", "svc", "power", "stayon", "true")
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("logcat", "-c")
    log = subprocess.Popen([ADB, "logcat", "-v", "raw", "-s", "AIROXE2E:I"], stdout=subprocess.PIPE, text=True)

    workers: list[threading.Thread] = []

    def run(fn, *a):
        try:
            fn(*a)
        except Exception as exc:
            print(f"  (harness error in {fn.__name__}{a}: {exc})", flush=True)

    def tail():
        for line in log.stdout:
            line = line.strip()
            if line.startswith("STEP "):
                print(f"  [phone] {line}", flush=True)
                t = threading.Thread(target=run, args=(react, line[5:]), daemon=True)
            elif line.startswith("RESULT "):
                k, _, v = line[7:].partition("=")
                print(f"  [phone] {k} = {v[:140]}", flush=True)
                t = threading.Thread(target=run, args=(on_result, k, v), daemon=True)
            else:
                continue
            workers.append(t)
            t.start()
    threading.Thread(target=tail, daemon=True).start()
    inst = subprocess.run([ADB, "shell", "am", "instrument", "-w", "-e", "class", "org.airux.connect.E2E",
                           "-e", "pairUri", shlex.quote(uri), "-e", "voiceWav", dev_voice, "-e", "pin", PIN,   # adb runs it in the phone's shell
                           "org.airux.connect.test/androidx.test.runner.AndroidJUnitRunner"],
                          capture_output=True, text=True, timeout=3600)
    adb("shell", "locksettings", "clear", "--old", PIN)
    time.sleep(3)
    for t in workers:
        t.join(120)
    log.terminate()
    out = inst.stdout
    tests_ok = re.search(r"OK \((\d+) tests\)", out)
    print("\n== instrumentation:", (tests_ok.group(0) if tests_ok else "FAILED"))
    if not tests_ok:
        print("\n".join(l for l in out.splitlines() if l.strip())[-3000:])
    if "latency_ms" not in facts:
        check("screen", False, "the second screen showed no frames on the phone")
    up = facts.get("pc_upload_sha", "")
    check("files", up and up == facts.get("upload_sha256"), f"phone->PC phone-photo.jpg SHA-256 on the PC {up[:12]}... "
          f"(phone {facts.get('upload_sha256', '')[:12]}...)")
    shots = sorted(p.name for p in SHOTS.glob("connect-phone-*.png"))
    print(f"phone screenshots: {shots}")
    items = ["pair", "aira", "tasks", "files", "clipboard", "input", "screen", "alerts", "refuse"]
    print()
    for i in items:
        print(f"{'PASS' if results.get(i) and all(results[i]) else 'FAIL'} {i}")
    ok = bool(tests_ok) and all(results.get(i) and all(results[i]) for i in items)
    print(f"\n=== AIROX Connect check: {'PASS' if ok else 'FAIL'} "
          f"{sum(1 for i in items if results.get(i) and all(results[i]))}/{len(items)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
