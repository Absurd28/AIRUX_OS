#!/usr/bin/env python3
"""Drive the AIRUX test VM: start QEMU, take screenshots, click, type, and run commands inside it.

    qemu_ctl.py start --iso X.iso [--disk D.qcow2] [--boot cd|disk]
    qemu_ctl.py shot out.png              screenshot (QMP screendump -> PNG)
    qemu_ctl.py click X Y                 left click at screen pixel (absolute, via usb-tablet)
    qemu_ctl.py type "text"               type text;   qemu_ctl.py key ret|tab|ctrl-alt-t ...
    qemu_ctl.py exec -- CMD ...           run a command in the guest (qemu-guest-agent), print output
    qemu_ctl.py put LOCAL REMOTE          copy a file into the guest;  get REMOTE LOCAL  copy out
    qemu_ctl.py ping | stop

Sockets and the VM's audio output (audio.wav) live in --dir (default $AIRUX_WORK/build/vm).
Stdlib only; run as root (KVM).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(os.environ.get("AIRUX_WORK", "/srv/airux-work")) / "build" / "vm"
SCREEN = (1280, 800)
OVMF_CODE = "/usr/share/OVMF/OVMF_CODE_4M.fd"
OVMF_VARS = "/usr/share/OVMF/OVMF_VARS_4M.fd"


class Chan:
    """Line-delimited JSON over a unix socket (QMP and QGA both speak this)."""

    def __init__(self, path: Path, timeout: float = 30):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.settimeout(timeout)
        self.s.connect(str(path))
        self.buf = b""

    def recv(self) -> dict:
        while b"\n" not in self.buf:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, execute: str, **arguments) -> dict:
        msg = {"execute": execute}
        if arguments:
            msg["arguments"] = arguments
        self.s.sendall(json.dumps(msg).encode() + b"\n")
        while True:
            r = self.recv()
            if "return" in r or "error" in r:
                if "error" in r:
                    raise RuntimeError(f"{execute}: {r['error']}")
                return r["return"]


def qmp(d: Path) -> Chan:
    c = Chan(d / "qmp.sock")
    c.recv()  # greeting
    c.cmd("qmp_capabilities")
    return c


def qga(d: Path, timeout: float = 30) -> Chan:
    c = Chan(d / "qga.sock", timeout)
    # resync: guest-sync-delimited, then discard anything before the delimiter
    tok = int(time.time()) % 100000
    c.s.sendall(b"\xff" + json.dumps({"execute": "guest-sync-delimited", "arguments": {"id": tok}}).encode() + b"\n")
    while True:
        b = c.s.recv(1)
        if b == b"\xff":
            break
    c.recv()
    return c


def start(d: Path, iso: Path | None, disk: Path, boot: str, mem: int = 6144, cpus: int = 4) -> None:
    d.mkdir(parents=True, exist_ok=True)
    if not disk.exists():
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(disk), "40G"], check=True)
    vars_ = d / "OVMF_VARS.fd"
    if not vars_.exists():
        vars_.write_bytes(Path(OVMF_VARS).read_bytes())
    for s in ("qmp.sock", "qga.sock"):
        (d / s).unlink(missing_ok=True)
    cmd = ["qemu-system-x86_64", "-enable-kvm", "-machine", "q35", "-cpu", "host", "-m", str(mem), "-smp", str(cpus),
           "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
           "-drive", f"if=pflash,format=raw,file={vars_}",
           "-drive", f"file={disk},if=virtio,format=qcow2",
           "-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0",
           "-device", "qemu-xhci", "-device", "usb-tablet", "-device", "usb-kbd",
           "-vga", "none", "-device", f"virtio-vga,xres={SCREEN[0]},yres={SCREEN[1]}",
           "-audiodev", f"wav,id=snd0,path={d / 'audio.wav'}",
           "-device", "intel-hda", "-device", "hda-output,audiodev=snd0",
           "-chardev", f"socket,path={d / 'qga.sock'},server=on,wait=off,id=qga0",
           "-device", "virtio-serial", "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
           "-qmp", f"unix:{d / 'qmp.sock'},server=on,wait=off",
           "-display", "none", "-daemonize", "-pidfile", str(d / "qemu.pid"),
           "-serial", f"file:{d / 'serial.log'}"]
    if iso is not None and boot == "cd":
        cmd += ["-drive", f"file={iso},media=cdrom,readonly=on,if=none,id=cd0",
                "-device", "ide-cd,drive=cd0,bootindex=0"]
    subprocess.run(cmd, check=True)
    print(f"VM started ({boot}); pid {(d / 'qemu.pid').read_text().strip()}")


def shot(d: Path, out: Path) -> None:
    ppm = d / "shot.ppm"
    qmp(d).cmd("screendump", filename=str(ppm))
    time.sleep(0.3)
    subprocess.run(["convert", str(ppm), str(out)], check=True)
    print(out)


def click(d: Path, x: int, y: int, button: str = "left") -> None:
    c = qmp(d)
    ax, ay = int(x * 32767 / (SCREEN[0] - 1)), int(y * 32767 / (SCREEN[1] - 1))
    c.cmd("input-send-event", events=[{"type": "abs", "data": {"axis": "x", "value": ax}},
                                      {"type": "abs", "data": {"axis": "y", "value": ay}}])
    for down in (True, False):
        c.cmd("input-send-event", events=[{"type": "btn", "data": {"down": down, "button": button}}])
        time.sleep(0.05)


KEYMAP = {" ": "spc", "\n": "ret", "\t": "tab", "-": "minus", "=": "equal", ".": "dot", ",": "comma",
          "/": "slash", ";": "semicolon", "'": "apostrophe", "[": "bracket_left", "]": "bracket_right",
          "\\": "backslash", "`": "grave_accent"}
SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
           "_": "minus", "+": "equal", ":": "semicolon", '"': "apostrophe", "?": "slash", "<": "comma", ">": "dot",
           "{": "bracket_left", "}": "bracket_right", "|": "backslash", "~": "grave_accent"}


def key(d: Path, combo: str) -> None:
    keys = [{"type": "qcode", "data": k} for k in combo.split("-")]
    qmp(d).cmd("send-key", keys=keys)
    time.sleep(0.08)


def type_text(d: Path, text: str) -> None:
    c = qmp(d)
    for ch in text:
        if ch.isalpha() and ch.isupper():
            keys = ["shift", ch.lower()]
        elif ch in SHIFTED:
            keys = ["shift", SHIFTED[ch]]
        else:
            keys = [KEYMAP.get(ch, ch)]
        c.cmd("send-key", keys=[{"type": "qcode", "data": k} for k in keys])
        time.sleep(0.04)


def gexec(d: Path, argv: list[str], timeout: float = 600, input_data: bytes | None = None) -> dict:
    c = qga(d)
    args = {"path": argv[0], "arg": argv[1:], "capture-output": True}
    if input_data is not None:
        args["input-data"] = base64.b64encode(input_data).decode()
    pid = c.cmd("guest-exec", **args)["pid"]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = c.cmd("guest-exec-status", pid=pid)
        if st.get("exited"):
            dec = lambda k: base64.b64decode(st.get(k, "")).decode(errors="replace")  # noqa: E731
            return {"exitcode": st.get("exitcode"), "out": dec("out-data"), "err": dec("err-data")}
        time.sleep(1)
    raise TimeoutError(f"guest command did not finish in {timeout:.0f}s: {argv}")


def put(d: Path, local: Path, remote: str) -> None:
    c = qga(d, timeout=120)
    h = c.cmd("guest-file-open", path=remote, mode="wb")
    data = local.read_bytes()
    for i in range(0, len(data), 48 * 1024):
        c.cmd("guest-file-write", handle=h, **{"buf-b64": base64.b64encode(data[i:i + 48 * 1024]).decode()})
    c.cmd("guest-file-close", handle=h)


def get(d: Path, remote: str, local: Path) -> None:
    c = qga(d, timeout=120)
    h = c.cmd("guest-file-open", path=remote, mode="rb")
    out = b""
    while True:
        r = c.cmd("guest-file-read", handle=h, count=48 * 1024)
        out += base64.b64decode(r["buf-b64"])
        if r.get("eof"):
            break
    c.cmd("guest-file-close", handle=h)
    local.write_bytes(out)


def ping(d: Path) -> bool:
    try:
        qga(d, timeout=5).cmd("guest-ping")
        return True
    except (OSError, RuntimeError, ConnectionError, TimeoutError):
        return False


def stop(d: Path) -> None:
    try:
        qmp(d).cmd("quit")
    except (OSError, ConnectionError, RuntimeError):
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qemu_ctl")
    ap.add_argument("--dir", type=Path, default=DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--iso", type=Path)
    s.add_argument("--disk", type=Path)
    s.add_argument("--boot", choices=["cd", "disk"], default="cd")
    sub.add_parser("shot").add_argument("out", type=Path)
    c = sub.add_parser("click")
    c.add_argument("x", type=int)
    c.add_argument("y", type=int)
    sub.add_parser("type").add_argument("text")
    sub.add_parser("key").add_argument("combo")
    e = sub.add_parser("exec")
    e.add_argument("argv", nargs=argparse.REMAINDER)
    p = sub.add_parser("put")
    p.add_argument("local", type=Path)
    p.add_argument("remote")
    g = sub.add_parser("get")
    g.add_argument("remote")
    g.add_argument("local", type=Path)
    sub.add_parser("ping")
    sub.add_parser("stop")
    a = ap.parse_args(argv)
    d = a.dir
    if a.cmd == "start":
        start(d, a.iso, a.disk or d / "airux.qcow2", a.boot)
    elif a.cmd == "shot":
        shot(d, a.out)
    elif a.cmd == "click":
        click(d, a.x, a.y)
    elif a.cmd == "type":
        type_text(d, a.text)
    elif a.cmd == "key":
        key(d, a.combo)
    elif a.cmd == "exec":
        argv_ = a.argv[1:] if a.argv and a.argv[0] == "--" else a.argv
        r = gexec(d, argv_)
        print(r["out"], end="")
        print(r["err"], end="", file=sys.stderr)
        return int(r["exitcode"] or 0)
    elif a.cmd == "put":
        put(d, a.local, a.remote)
    elif a.cmd == "get":
        get(d, a.remote, a.local)
    elif a.cmd == "ping":
        ok = ping(d)
        print("guest agent answers" if ok else "no answer")
        return 0 if ok else 1
    elif a.cmd == "stop":
        stop(d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
