"""AIROX Connect's actions as broker tools (caller "connect"), risk computed in code:

  send_files ........... send_off_machine (asks first: PC files leave the PC)
  receive_file ......... write (a new file in ~/Downloads/AIROX Connect; never overwrites)
  clipboard_get ........ read;   clipboard_set ........ write
  open_link ............ send_off_machine (NEOX contacts the website; asks first)
  media ................ write (play/pause, next, previous, volume)
  power ................ lock: write;  sleep, restart, shut down: stop (asks first)
  screenshot ........... read (a glance at the screen, shown only on the paired phone)
  remote_input ......... write (touchpad and keyboard; KDE asks the person at the PC once)
  screen_stream ........ write (second screen or mirror; KDE asks the person at the PC once)
The Task Manager's own tools (end task, services, ...) are in airux_taskmgr.actions.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from aira.tools.base import Registry, Risk, S, Tool, ToolError

CALLER = "connect"
MEDIA = {"play_pause": ["playerctl", "play-pause"], "next": ["playerctl", "next"],
         "previous": ["playerctl", "previous"], "stop": ["playerctl", "stop"]}


def volume() -> dict:
    r = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], capture_output=True, text=True, timeout=5)
    m = re.search(r"Volume:\s*([0-9.]+)", r.stdout)
    if not m:
        raise ToolError("the sound system did not answer (wpctl)")
    return {"volume": round(float(m.group(1)) * 100), "muted": "MUTED" in r.stdout}


def media(action: str, level: int | None = None) -> dict:
    if action in MEDIA:
        if not shutil.which("playerctl"):
            raise ToolError("playerctl is not installed")
        r = subprocess.run(MEDIA[action], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            raise ToolError((r.stderr or r.stdout).strip() or "no music or video player is running")
        time.sleep(0.2)
        st = subprocess.run(["playerctl", "metadata", "--format", "{{status}}|{{artist}}|{{title}}"],
                            capture_output=True, text=True, timeout=5).stdout.strip().split("|")
        return {"status": st[0] if st else "", "artist": st[1] if len(st) > 1 else "",
                "title": st[2] if len(st) > 2 else "", "verified": True}
    before = volume()
    sink = "@DEFAULT_AUDIO_SINK@"
    if action not in ("volume_up", "volume_down", "mute", "set_volume"):
        raise ToolError(f"unknown media action {action}")
    if action == "mute":
        subprocess.run(["wpctl", "set-mute", sink, "toggle"], capture_output=True, timeout=5)
        after = volume()
    else:
        # absolute targets: a relative 5 % step can be rounded back to the same value by a hardware mixer,
        # so step further (10 %, 15 %) until the read-back moves
        steps = [max(0, min(100, level or 0))] if action == "set_volume" else [
            before["volume"] + (d if action == "volume_up" else -d) for d in (5, 10, 15)]
        for target in steps:
            target = max(0, min(100, target))
            subprocess.run(["wpctl", "set-volume", "-l", "1.0", sink, f"{target / 100:.2f}"], capture_output=True, timeout=5)
            after = volume()
            if action == "set_volume" or after["volume"] != before["volume"] or target in (0, 100):
                break
    want_ok = {"volume_up": after["volume"] >= before["volume"], "volume_down": after["volume"] <= before["volume"],
               "mute": after["muted"] != before["muted"],
               "set_volume": abs(after["volume"] - max(0, min(100, level or 0))) <= 1}[action]
    return {**after, "verified": want_ok}


def power(action: str) -> dict:
    cmd = {"lock": ["loginctl", "lock-session"], "sleep": ["systemctl", "suspend"],
           "restart": ["systemctl", "reboot"], "shutdown": ["systemctl", "poweroff"]}.get(action)
    if cmd is None:
        raise ToolError(f"unknown power action {action}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise ToolError((r.stderr or r.stdout).strip()[-300:] or f"{action} failed")
    if action == "lock":
        time.sleep(1)
        s = subprocess.run(["loginctl", "show-session", os.environ.get("XDG_SESSION_ID", "auto"), "-p", "LockedHint",
                            "--value"], capture_output=True, text=True, timeout=5).stdout.strip()
        return {"action": action, "verified": s == "yes", "detail": "the PC is locked" if s == "yes" else
                "the lock was asked for, but the session does not report locked"}
    return {"action": action, "verified": True, "detail": f"{action} started"}


def screenshot(out_dir: Path) -> dict:
    """Spectacle first (KWin's screenshot interface); if it fails, one frame through the ScreenCast
    portal (the mirror's session: KDE asks once which screen)."""
    for f in out_dir.glob("glance-*"):
        f.unlink(missing_ok=True)
    path = out_dir / f"glance-{int(time.time() * 1000)}.jpg"
    why = ""
    exe = shutil.which("spectacle")
    if exe:
        try:
            r = subprocess.run([exe, "--background", "--nonotify", "--fullscreen", "--output", str(path)],
                               capture_output=True, text=True, timeout=20)
            why = r.stderr.strip()[-200:]
        except subprocess.TimeoutExpired:
            why = "Spectacle did not answer"
    if not path.exists() or path.stat().st_size == 0:
        from .helpers import helper_cmd, helper_env
        try:
            r = subprocess.run(helper_cmd("snapshot", "--output", str(path)), capture_output=True, text=True,
                               timeout=90, env=helper_env())
            why = r.stderr.strip()[-200:] or why
        except subprocess.TimeoutExpired:
            why = "the screen-sharing portal did not answer"
    if not path.exists() or path.stat().st_size == 0:
        raise ToolError("the screen could not be captured" + (f": {why}" if why else ""))
    return {"path": str(path), "size": path.stat().st_size}


def open_link(url: str) -> dict:
    if not re.match(r"^https?://[^\s/$.?#][^\s]*$", url or "", re.I) or len(url) > 4000:
        raise ToolError("only web links (http or https) can be opened")
    exe = shutil.which("neox") or shutil.which("xdg-open")
    if not exe:
        raise ToolError("no browser is installed")
    subprocess.Popen([exe, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"url": url, "browser": os.path.basename(exe)}


def build(svc) -> Registry:
    """`svc` is the running Service (server.py): transfers, the desktop helper, streams."""
    from aira.tools import desktop as D
    reg = Registry(agents=(CALLER,))
    A = CALLER
    arr = {"type": "array", "items": {"type": "string"}, "maxItems": 50, "description": "paths in the home folder"}

    def send_files(paths: list[str], device: str) -> dict:
        from .files import FilesError
        try:
            offers = svc.transfers.offer(device, paths, origin=svc.ctx_origin())
        except FilesError as exc:
            raise ToolError(str(exc)) from exc
        return {"offers": offers, "verified": True}

    def receive_file(name: str, size: str) -> dict:
        return {"folder": str(svc.transfers.inbox), "name": name, "size": size}

    def clipboard_get() -> dict:
        return D.clipboard("get")

    def clipboard_set(text: str) -> dict:
        D.clipboard("set", text)
        got = None
        for _ in range(20):                  # wl-copy hands the text over in the background
            got = D.clipboard("get")["text"]
            if got == text:
                break
            time.sleep(0.1)
        return {"verified": got == text, "chars": len(text)}

    def remote_input(action: str) -> dict:
        if not svc.desktop.send({"op": "input_start" if action == "start" else "input_stop"}):
            raise ToolError("remote input is not available on this PC (no desktop session)")
        return {"state": "asked"}

    def screen_stream(source: str) -> dict:
        return {"source": source}

    reg.add(Tool("send_files", A, "send files from the PC to the phone", {"paths": arr, "device": S("device id")},
                 ["paths", "device"], send_files, frozenset({Risk.SEND_OFF})))
    reg.add(Tool("receive_file", A, "save a file sent by the phone", {"name": S("file name"), "size": S("bytes")},
                 ["name", "size"], receive_file, frozenset({Risk.WRITE})))
    reg.add(Tool("clipboard_get", A, "read the PC's clipboard", {}, [], clipboard_get))
    reg.add(Tool("clipboard_set", A, "put text on the PC's clipboard", {"text": S("text")}, ["text"], clipboard_set,
                 frozenset({Risk.WRITE}), changes_state=True))
    reg.add(Tool("open_link", A, "open a web link in NEOX on the PC", {"url": S("http(s) link")}, ["url"], open_link,
                 frozenset({Risk.SEND_OFF})))
    reg.add(Tool("media", A, "media keys and volume", {
        "action": S("what", enum=["play_pause", "next", "previous", "stop", "volume_up", "volume_down", "mute", "set_volume"]),
        "level": {"type": "integer", "minimum": 0, "maximum": 100}}, ["action"], media, frozenset({Risk.WRITE}),
        changes_state=True))
    reg.add(Tool("power", A, "lock, sleep, restart or shut down the PC",
                 {"action": S("what", enum=["lock", "sleep", "restart", "shutdown"])}, ["action"], power,
                 lambda a: {Risk.WRITE} if a.get("action") == "lock" else {Risk.STOP}, changes_state=True))
    reg.add(Tool("screenshot", A, "a picture of the PC's screen for the phone", {}, [],
                 lambda: screenshot(svc.runtime)))
    reg.add(Tool("remote_input", A, "use the phone as touchpad and keyboard",
                 {"action": S("start or stop", enum=["start", "stop"])}, ["action"], remote_input, frozenset({Risk.WRITE})))
    reg.add(Tool("screen_stream", A, "show the PC's second screen or a mirror on the phone",
                 {"source": S("virtual (second screen) or monitor (mirror)", enum=["virtual", "monitor"])}, ["source"],
                 screen_stream, frozenset({Risk.WRITE})))
    return reg


def describe(tool: str, args: dict, svc=None) -> str:
    if tool == "send_files":
        names = [Path(p).name for p in args.get("paths", [])]
        shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        dev = svc.devices.get(args.get("device", "")) if svc else None
        return f"Send {shown} from the PC to {dev['name'] if dev else 'the phone'}?"
    if tool == "open_link":
        return f"Open {args.get('url', '')[:120]} in NEOX on the PC? NEOX will contact that website."
    if tool == "power":
        a = args.get("action")
        return {"sleep": "Put the PC to sleep?", "restart": "Restart the PC? Anything not saved is lost.",
                "shutdown": "Shut down the PC? Anything not saved is lost."}.get(a, f"{a} the PC?")
    return f"{tool} {args}"
