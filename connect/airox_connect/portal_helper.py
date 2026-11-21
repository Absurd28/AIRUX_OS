"""AIROX Connect's desktop helper. Runs with Debian's python3 (python3-dbus, python3-gi), because AIRA's
pinned virtual environment has no D-Bus bindings. It speaks JSON lines with airox-connectd.

  python3 -m airox_connect.portal_helper daemon
      stdin  {"op": "input_start"} | {"op": "pointer", "dx", "dy"} | {"op": "button", "button": "left"|"right"|"middle",
             "down": bool} | {"op": "scroll", "dx", "dy"} | {"op": "key", "keysym": int, "down": bool} |
             {"op": "text", "text": str} | {"op": "input_stop"} |
             {"op": "abs", "output": name|null, "x": 0..1, "y": 0..1, "phase": "down"|"move"|"up", "press": bool}
             (a touch on the second screen or the mirror) | {"op": "place_virtual", "output": name}
      stdout {"ev": "input", "state": "ready"|"denied"|"closed"|"error", ...} |
             {"ev": "notification", "app", "title", "body", "urgency"}       (desktop notifications, mirrored)

  python3 -m airox_connect.portal_helper stream --source virtual|monitor --fps F --kbps B [--encoder E]
      stdout the H.264 byte stream (Annex B, no B-frames, SPS/PPS before every key frame) from GStreamer
      stderr JSON status lines prefixed "@@" ({"ev": "stream", "output", "width", "height", "encoder"} or errors)

Input uses one xdg-desktop-portal RemoteDesktop session; the stream a ScreenCast session whose chooser
offers KDE's "New Virtual Output" (the second screen). KDE asks the person at the PC once for each;
the portal's restore tokens are kept so it does not ask again. Spike results (Plasma 6.3, Debian 13):
the virtual output is 1920x1080 (KDE's fixed size); relative pointer moves are exact inside one output.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
KEYBOARD, POINTER = 1, 2
MONITOR, VIRTUAL = 1, 4
BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}       # evdev BTN_LEFT, BTN_RIGHT, BTN_MIDDLE


def state_dir() -> Path:
    d = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "airox-connect"
    d.mkdir(parents=True, exist_ok=True)
    return d


def out(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def status(msg: dict) -> None:
    sys.stderr.write("@@" + json.dumps(msg) + "\n")
    sys.stderr.flush()


class Portal:
    def __init__(self, bus: dbus.SessionBus):
        self.bus = bus
        obj = bus.get_object(BUS, PATH)
        self.rd = dbus.Interface(obj, "org.freedesktop.portal.RemoteDesktop")
        self.sc = dbus.Interface(obj, "org.freedesktop.portal.ScreenCast")
        self.sender = bus.get_unique_name()[1:].replace(".", "_")
        self.n = 0
        self.session = None

    def _call(self, method, *args, options: dict, done) -> None:
        """Portal calls answer with a Response signal on a Request object."""
        self.n += 1
        token = f"airox{os.getpid()}_{self.n}"
        options["handle_token"] = token
        path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"

        def on_response(code, results):
            match.remove()
            done(int(code), results)
        match = self.bus.add_signal_receiver(on_response, "Response", "org.freedesktop.portal.Request", BUS, path)
        method(*args, options)

    def start(self, with_stream: bool, source: int, token_name: str, done) -> None:
        """CreateSession -> SelectDevices [-> SelectSources] -> Start; done(ok, results or error)."""
        tok_file = state_dir() / f"{token_name}.restore"
        restore = tok_file.read_text().strip() if tok_file.exists() else ""

        def created(code, res):
            if code != 0:
                return done(False, "the desktop did not create a session")
            self.session = res["session_handle"]
            opts = {"types": dbus.UInt32(KEYBOARD | POINTER), "persist_mode": dbus.UInt32(2)}
            if restore:
                opts["restore_token"] = restore
            self._call(self.rd.SelectDevices, self.session, options=opts, done=selected)

        def selected(code, res):
            if code != 0:
                return done(False, "remote input was not allowed")
            if with_stream:
                self._call(self.sc.SelectSources, self.session, options={
                    "types": dbus.UInt32(source), "multiple": False, "cursor_mode": dbus.UInt32(2)}, done=sources)
            else:
                begin()

        def sources(code, res):
            if code != 0:
                return done(False, "the screen was not shared")
            begin()

        def begin():
            self._call(self.rd.Start, self.session, "", options={}, done=started)

        def started(code, res):
            if code != 0:
                return done(False, "the person at the PC did not allow it" if code == 1 else "the desktop refused")
            if res.get("restore_token"):
                tok_file.write_text(str(res["restore_token"]))
                os.chmod(tok_file, 0o600)
            done(True, res)

        self._call(self.rd.CreateSession, options={"session_handle_token": f"airoxs{os.getpid()}"}, done=created)

    def start_screencast(self, token_name: str, done) -> None:
        """A ScreenCast-only session: KDE's chooser lists the screens and "New Virtual Output" (a new
        monitor, which is how the phone or tablet becomes a second screen). The choice is remembered."""
        tok_file = state_dir() / f"{token_name}.restore"
        restore = tok_file.read_text().strip() if tok_file.exists() else ""

        def created(code, res):
            if code != 0:
                return done(False, "the desktop did not create a session")
            self.session = res["session_handle"]
            opts = {"types": dbus.UInt32(MONITOR), "multiple": False, "cursor_mode": dbus.UInt32(2),
                    "persist_mode": dbus.UInt32(2)}
            if restore:
                opts["restore_token"] = restore
            self._call(self.sc.SelectSources, self.session, options=opts, done=selected)

        def selected(code, res):
            if code != 0:
                return done(False, "the screen was not shared")
            self._call(self.sc.Start, self.session, "", options={}, done=started)

        def started(code, res):
            if code != 0:
                return done(False, "the person at the PC did not allow it" if code == 1 else "the desktop refused")
            if res.get("restore_token"):
                tok_file.write_text(str(res["restore_token"]))
                os.chmod(tok_file, 0o600)
            done(True, res)

        self._call(self.sc.CreateSession, options={"session_handle_token": f"airoxc{os.getpid()}"}, done=created)

    # ---- input
    def motion(self, dx: float, dy: float) -> None:
        self.rd.NotifyPointerMotion(self.session, {}, float(dx), float(dy))

    def absolute(self, stream: int, x: float, y: float) -> None:
        self.rd.NotifyPointerMotionAbsolute(self.session, {}, dbus.UInt32(stream), float(x), float(y))

    def button(self, name: str, down: bool) -> None:
        self.rd.NotifyPointerButton(self.session, {}, dbus.Int32(BUTTONS.get(name, BUTTONS["left"])),
                                    dbus.UInt32(1 if down else 0))

    def scroll(self, dx: float, dy: float) -> None:
        self.rd.NotifyPointerAxis(self.session, {"finish": True}, float(dx), float(dy))

    def key(self, keysym: int, down: bool) -> None:
        self.rd.NotifyKeyboardKeysym(self.session, {}, dbus.Int32(keysym), dbus.UInt32(1 if down else 0))

    def text(self, text: str) -> None:
        for ch in text:
            ks = keysym_for(ch)
            self.key(ks, True)
            self.key(ks, False)

    def close(self) -> None:
        if self.session:
            try:
                dbus.Interface(self.bus.get_object(BUS, self.session), "org.freedesktop.portal.Session").Close()
            except dbus.DBusException:
                pass
            self.session = None


def keysym_for(ch: str) -> int:
    special = {"\n": 0xFF0D, "\r": 0xFF0D, "\t": 0xFF09, "\b": 0xFF08}
    if ch in special:
        return special[ch]
    o = ord(ch)
    return o if 0x20 <= o <= 0x7E or 0xA0 <= o <= 0xFF else 0x01000000 + o   # X keysyms: Latin-1, else Unicode


def outputs() -> list[dict]:
    """Enabled outputs in logical (scaled) coordinates, from kscreen-doctor."""
    try:
        r = subprocess.run(["kscreen-doctor", "-j"], capture_output=True, text=True, timeout=5)
        data = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    outs = []
    for o in data.get("outputs", []):
        if not o.get("enabled") or not o.get("size"):
            continue
        sc = float(o.get("scale") or 1)
        w, h = o["size"]["width"], o["size"]["height"]
        if o.get("rotation") in (2, 8):
            w, h = h, w
        outs.append({"name": o["name"], "x": o["pos"]["x"], "y": o["pos"]["y"], "w": round(w / sc), "h": round(h / sc)})
    return outs


class Placer:
    """Puts the pointer at a point on one output with relative moves only.

    KWin applies relative moves exactly inside one output but clamps a move whose target is off-screen
    to the current output's edge, and a jump between outputs is not exact. So on a touch-down the
    pointer walks edge to edge (up, then sideways, a step across each border) to the target output's
    outer top corner, then moves inside it. The second screen is placed rightmost with its top at 0
    (place_virtual), so this reaches it; mirror mode targets the leftmost screen."""

    def __init__(self, portal: Portal):
        self.p = portal
        self.cache: tuple[float, list[dict]] = (0.0, [])
        self.cur: tuple[str, float, float] | None = None

    def layout(self) -> list[dict]:
        import time
        t, outs = self.cache
        if time.monotonic() - t > 3 or not outs:
            outs = outputs()
            self.cache = (time.monotonic(), outs)
        return outs

    def target(self, name: str | None) -> dict | None:
        outs = self.layout()
        if not outs:
            return None
        if name:
            return next((o for o in outs if o["name"] == name), None)
        return min(outs, key=lambda o: (o["x"], o["y"]))

    def place(self, name: str | None, fx: float, fy: float) -> None:
        o = self.target(name)
        if o is None:
            return
        outs = self.layout()
        tx, ty = min(max(fx, 0.0), 1.0) * (o["w"] - 1), min(max(fy, 0.0), 1.0) * (o["h"] - 1)
        rightmost = o["x"] + o["w"] >= max(q["x"] + q["w"] for q in outs)
        step = 100000 if rightmost else -100000
        for _ in range(len(outs)):
            self.p.motion(0, -100000)
            self.p.motion(step, 0)
            self.p.motion(2 if rightmost else -2, 0)
        self.p.motion(0, -100000)
        self.p.motion(step, 0)
        cx = o["w"] - 1 if rightmost else 0
        self.p.motion(tx - cx, ty)
        self.cur = (o["name"], tx, ty)

    def move(self, name: str | None, fx: float, fy: float) -> None:
        o = self.target(name)
        if o is None or self.cur is None or self.cur[0] != o["name"]:
            return self.place(name, fx, fy)
        tx, ty = min(max(fx, 0.0), 1.0) * (o["w"] - 1), min(max(fy, 0.0), 1.0) * (o["h"] - 1)
        self.p.motion(tx - self.cur[1], ty - self.cur[2])
        self.cur = (o["name"], tx, ty)


def place_virtual(name: str) -> None:
    """Put the second screen to the right of every other screen, top at 0 (see Placer)."""
    others = [o for o in outputs() if o["name"] != name]
    if others:
        x = max(o["x"] + o["w"] for o in others)
        subprocess.run(["kscreen-doctor", f"output.{name}.position.{x},0"], capture_output=True, timeout=10)


def handle_input(p: Portal, m: dict, placer: Placer | None = None) -> None:
    op = m.get("op")
    if op == "pointer":
        if placer:
            placer.cur = None
        p.motion(m.get("dx", 0), m.get("dy", 0))
    elif op == "abs" and placer is not None:
        out_name = m.get("output") or None
        if m.get("phase") == "down":
            placer.place(out_name, float(m["x"]), float(m["y"]))
            if m.get("press", True):
                p.button("left", True)
        elif m.get("phase") == "up":
            placer.move(out_name, float(m["x"]), float(m["y"]))
            if m.get("press", True):
                p.button("left", False)
        else:
            placer.move(out_name, float(m["x"]), float(m["y"]))
    elif op == "button":
        p.button(m.get("button", "left"), bool(m.get("down")))
    elif op == "scroll":
        p.scroll(m.get("dx", 0), m.get("dy", 0))
    elif op == "key":
        p.key(int(m["keysym"]), bool(m.get("down")))
    elif op == "text":
        p.text(str(m.get("text", ""))[:2000])


def stdin_lines(loop: GLib.MainLoop, on_line) -> None:
    """Read JSON lines from stdin in a thread; hand each to the GLib main loop."""
    def reader():
        for line in sys.stdin:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            GLib.idle_add(lambda m=m: (on_line(m), False)[1])
        GLib.idle_add(loop.quit)
    threading.Thread(target=reader, daemon=True).start()


# ------------------------------------------------------------------ daemon: input + notifications
_MONITOR = []          # the monitor connection must stay referenced, or it is closed at once


def notification_monitor(loop: GLib.MainLoop) -> None:
    """Mirror desktop notifications: a private monitor connection (BecomeMonitor) to the session bus."""
    try:
        mon = dbus.SessionBus(private=True)
        rule = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
        mon.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus").BecomeMonitor(
            [rule], dbus.UInt32(0), dbus_interface="org.freedesktop.DBus.Monitoring")
    except dbus.DBusException as exc:
        out({"ev": "notification_monitor", "ok": False, "error": str(exc)})
        return

    def on_msg(bus, msg):
        if msg.get_member() == "Notify":
            a = msg.get_args_list()
            if len(a) >= 8:
                hints = a[6] or {}
                urg = int(hints.get("urgency", 1)) if "urgency" in hints else 1
                out({"ev": "notification", "app": str(a[0]), "title": str(a[3]), "body": str(a[4]),
                     "urgency": ["low", "normal", "critical"][min(max(urg, 0), 2)]})
    mon.add_message_filter(on_msg)
    _MONITOR.append(mon)
    out({"ev": "notification_monitor", "ok": True})


def daemon() -> int:
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    loop = GLib.MainLoop()
    portal = Portal(bus)
    placer = Placer(portal)
    ready = {"on": False, "starting": False}
    notification_monitor(loop)

    def on_line(m: dict) -> None:
        op = m.get("op")
        try:
            if op == "input_start":
                if ready["on"]:
                    return out({"ev": "input", "state": "ready"})
                if ready["starting"]:
                    return

                def done(ok, res):
                    ready["on"], ready["starting"] = ok, False
                    out({"ev": "input", "state": "ready" if ok else "denied", **({} if ok else {"error": res})})
                ready["starting"] = True
                portal.start(False, 0, "input", done)
            elif op == "input_stop":
                portal.close()
                ready["on"] = False
                out({"ev": "input", "state": "closed"})
            elif op == "place_virtual":
                place_virtual(str(m.get("output", "")))
                placer.cache = (0.0, [])
            elif ready["on"]:
                handle_input(portal, m, placer)
        except dbus.DBusException as exc:
            ready["on"] = False
            out({"ev": "input", "state": "error", "error": exc.get_dbus_message() or str(exc)})
    stdin_lines(loop, on_line)
    loop.run()
    portal.close()
    return 0


# ------------------------------------------------------------------ stream: second screen / mirror
def encoder(kbps: int, fps: int, prefer: str = "auto") -> tuple[str, str]:
    """The lowest-latency H.264 encoder GStreamer has here (NVENC, then VA-API, then x264)."""
    def have(el):
        return subprocess.run(["gst-inspect-1.0", "--exists", el]).returncode == 0
    gop = max(fps * 2, 30)
    if prefer in ("auto", "nvenc") and have("nvh264enc") and os.path.exists("/dev/nvidia0"):
        return "nvh264enc", (f"cudaupload ! nvh264enc preset=low-latency-hq rc-mode=cbr bitrate={kbps} gop-size={gop} "
                             f"bframes=0 zerolatency=true")
    if prefer in ("auto", "vaapi") and (have("vah264lpenc") or have("vah264enc")) and os.path.exists("/dev/dri/renderD128"):
        el = "vah264lpenc" if have("vah264lpenc") else "vah264enc"
        return el, f"vapostproc ! {el} bitrate={kbps} key-int-max={gop} b-frames=0 target-usage=7"
    return "x264enc", (f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={kbps} key-int-max={gop} "
                       f"bframes=0 sliced-threads=true")


def stream(args) -> int:
    """Video only: the second screen ("New Virtual Output", chosen once at the PC) or a mirror of a screen.
    Touches go through the daemon's input session (one consent for everything)."""
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    loop = GLib.MainLoop()
    portal = Portal(bus)
    st: dict = {"proc": None}
    before = {o["name"] for o in outputs()}

    def done(ok, res):
        if not ok:
            status({"ev": "error", "error": res})
            return loop.quit()
        streams = res.get("streams") or []
        if not streams:
            status({"ev": "error", "error": "the desktop gave no screen"})
            return loop.quit()
        node, props = int(streams[0][0]), dict(streams[0][1])
        size = props.get("size")
        w, h = (int(size[0]), int(size[1])) if size else (args.width, args.height)
        fd = portal.sc.OpenPipeWireRemote(portal.session, {}).take()
        name, enc = encoder(args.kbps, args.fps, args.encoder)
        pipe = (f"pipewiresrc fd={fd} path={node} do-timestamp=true keepalive-time=500 always-copy=false ! "
                f"videorate drop-only=true ! video/x-raw,framerate={args.fps}/1 ! videoconvert ! "
                f"video/x-raw,format=NV12 ! {enc} ! h264parse config-interval=-1 ! "
                f"video/x-h264,stream-format=byte-stream,alignment=au ! fdsink fd=1 sync=false")
        st["proc"] = subprocess.Popen(["gst-launch-1.0", "-q", *pipe.split()], stdout=sys.stdout.fileno(),
                                      stderr=subprocess.PIPE, pass_fds=(fd,), text=True)
        os.close(fd)
        # which output is it? the new one (a virtual output), or the one at the stream's position
        output, pos = None, [int(x) for x in props.get("position", (0, 0))]
        for _ in range(20):
            outs = outputs()
            new = [o for o in outs if o["name"] not in before]
            if args.source == "virtual" and new:
                output = new[0]["name"]
                break
            if args.source == "monitor":
                output = next((o["name"] for o in outs if [o["x"], o["y"]] == pos), outs[0]["name"] if outs else None)
                break
            import time
            time.sleep(0.25)
        status({"ev": "stream", "node": node, "width": w, "height": h, "encoder": name, "source": args.source,
                "output": output, "virtual": bool(args.source == "virtual" and output and output not in before)})

        def watch():
            err = st["proc"].stderr.read()
            rc = st["proc"].wait()
            status({"ev": "ended", "code": rc, "error": err.strip()[-600:]})
            GLib.idle_add(loop.quit)
        threading.Thread(target=watch, daemon=True).start()

    portal.start_screencast(f"screencast-{args.source}", done)
    stdin_lines(loop, lambda m: None)
    loop.run()
    if st["proc"] and st["proc"].poll() is None:
        st["proc"].terminate()
    portal.close()
    return 0


def snapshot(args) -> int:
    """One JPEG of a screen through the ScreenCast portal (the mirror's session and choice)."""
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    loop = GLib.MainLoop()
    portal = Portal(bus)
    st = {"rc": 1}

    def done(ok, res):
        if not ok or not res.get("streams"):
            status({"ev": "error", "error": res if not ok else "the desktop gave no screen"})
            return loop.quit()
        node = int(res["streams"][0][0])
        fd = portal.sc.OpenPipeWireRemote(portal.session, {}).take()
        pipe = (f"pipewiresrc fd={fd} path={node} num-buffers=3 ! videoconvert ! jpegenc quality=80 ! "
                f"multifilesink location={args.output} max-files=1")
        r = subprocess.run(["gst-launch-1.0", "-q", *pipe.split()], pass_fds=(fd,), capture_output=True, text=True,
                           timeout=60)
        os.close(fd)
        st["rc"] = 0 if r.returncode == 0 and os.path.exists(args.output) else 1
        if st["rc"]:
            status({"ev": "error", "error": r.stderr.strip()[-300:]})
        loop.quit()

    portal.start_screencast("screencast-monitor", done)
    GLib.timeout_add_seconds(80, loop.quit)
    loop.run()
    portal.close()
    return st["rc"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="airox_connect.portal_helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daemon")
    s = sub.add_parser("stream")
    s.add_argument("--source", choices=["virtual", "monitor"], default="virtual")
    s.add_argument("--width", type=int, default=1920)
    s.add_argument("--height", type=int, default=1080)
    s.add_argument("--fps", type=int, default=60)
    s.add_argument("--kbps", type=int, default=12000)
    s.add_argument("--encoder", choices=["auto", "nvenc", "vaapi", "x264"], default="auto")
    n = sub.add_parser("snapshot")
    n.add_argument("--output", required=True)
    a = ap.parse_args(argv)
    if not shutil.which("gst-launch-1.0") and a.cmd in ("stream", "snapshot"):
        status({"ev": "error", "error": "GStreamer is not installed"})
        return 1
    return {"daemon": lambda: daemon(), "stream": lambda: stream(a), "snapshot": lambda: snapshot(a)}[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
