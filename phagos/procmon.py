"""Processes as antigens: who they are, who is responsible for what they do, and their signals.

A ransomware script often does its damage through trusted tools (openssl, gpg, cp), so file writes
are charged to the *responsible* process: walking up the parent chain past package-owned tools to
the first program that is not from a package, or to an interpreter running a script that is not
from a package. A user's own `cp` in a terminal stays charged to `cp`.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

CLK = os.sysconf("SC_CLK_TCK")
INTERPRETERS = {"python", "python3", "perl", "ruby", "node", "nodejs", "php", "lua", "bash", "sh", "dash", "zsh",
                "fish", "ksh", "tclsh", "java", "busybox", "pwsh", "osascript"}
# never paused: the desktop, the session plumbing and Phagos itself
PROTECTED = {"systemd", "init", "kwin_wayland", "kwin_x11", "plasmashell", "Xwayland", "Xorg", "sddm", "sddm-helper",
             "dbus-daemon", "dbus-broker", "pipewire", "wireplumber", "pipewire-pulse", "polkitd", "NetworkManager",
             "systemd-logind", "systemd-journald", "udevd", "systemd-udevd", "phagosd", "kded6", "ksmserver",
             "startplasma-wayland", "plasma_session"}
RISKY_EXE = ("/tmp/", "/dev/shm/", "/var/tmp/", "/run/user/")


@dataclass
class Proc:
    pid: int
    ppid: int
    uid: int
    name: str
    exe: str
    cmdline: list[str]
    start: int                  # clock ticks after boot; with pid, a unique antigen id
    cpu: int                    # utime + stime, ticks
    deleted: bool = False

    @property
    def key(self) -> str:
        return f"{self.pid}:{self.start}"


def read(pid: int, root: str = "/proc") -> Proc | None:
    try:
        stat = Path(f"{root}/{pid}/stat").read_text()
        status = Path(f"{root}/{pid}/status").read_text()
        cmd = Path(f"{root}/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    name = stat[stat.find("(") + 1:stat.rfind(")")]
    f = stat[stat.rfind(")") + 2:].split()
    uid = next((int(l.split()[1]) for l in status.splitlines() if l.startswith("Uid:")), -1)
    try:
        exe = os.readlink(f"{root}/{pid}/exe")
    except OSError:
        exe = ""
    deleted = exe.endswith(" (deleted)")
    return Proc(pid, int(f[1]), uid, name, exe.removesuffix(" (deleted)"),
                [a.decode(errors="replace") for a in cmd.split(b"\0") if a], int(f[19]), int(f[11]) + int(f[12]), deleted)


def environ_has(pid: int, var: bytes) -> bool:
    try:
        return any(e.startswith(var + b"=") for e in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"))
    except OSError:
        return False


def sockets(pid: int) -> int:
    try:
        return sum(1 for fd in os.listdir(f"/proc/{pid}/fd") if os.readlink(f"/proc/{pid}/fd/{fd}").startswith("socket:"))
    except OSError:
        return 0


class Ownership:
    """Which files Debian packages installed (from dpkg's own lists)."""

    def __init__(self, info_dir: str = "/var/lib/dpkg/info", extra: set[str] | None = None):
        self.info_dir = info_dir
        self.md5: dict[str, str] | None = None        # loaded on first use
        self.paths: set[str] = set(extra or ())
        for lst in Path(info_dir).glob("*.list"):
            try:
                self.paths.update(lst.read_text(errors="replace").splitlines())
            except OSError:
                continue
        # /bin -> /usr/bin and friends (merged /usr)
        self.paths |= {"/usr" + p for p in self.paths if p.startswith(("/bin/", "/sbin/", "/lib/", "/lib64/"))}

    def owned(self, path: str) -> bool:
        return bool(path) and (path in self.paths or os.path.realpath(path) in self.paths)

    def unchanged(self, path: str, fd: int | None = None) -> bool:
        """A package's file, byte for byte as the package installed it (dpkg's own MD5 list).
        Phagos never quarantines or blocks such a file over a rule match: the match is a false
        alarm in a Debian package, not something that arrived later."""
        if not self.owned(path):
            return False
        if self.md5 is None:
            self.md5 = {}
            for f in Path(self.info_dir).glob("*.md5sums"):
                try:
                    for line in f.read_text(errors="replace").splitlines():
                        digest, _, rel = line.partition("  ")
                        if rel:
                            self.md5["/" + rel] = digest
                except OSError:
                    continue
        real = os.path.realpath(path)
        want = self.md5.get(path) or self.md5.get(real) or self.md5.get(real.removeprefix("/usr"))
        if not want:
            return False
        import hashlib
        h = hashlib.md5(usedforsecurity=False)
        try:
            if fd is not None:
                off = 0
                while chunk := os.pread(fd, 1 << 20, off):
                    h.update(chunk)
                    off += len(chunk)
            else:
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
        except OSError:
            return False
        return h.hexdigest() == want


def interpreter(p: Proc) -> bool:
    base = os.path.basename(p.exe).rstrip("0123456789.") or p.name
    return base in INTERPRETERS or p.name in INTERPRETERS


def script_of(p: Proc) -> str | None:
    """The script file an interpreter is running, if any (first argument that is an existing file)."""
    for a in p.cmdline[1:4]:
        if a.startswith("-"):
            continue
        if os.path.isfile(a) or (a.startswith("/") and os.path.isfile(a)):
            return os.path.abspath(a)
        break
    return None


def trusted(p: Proc, own: Ownership) -> bool:
    """A package's own program doing its own job (an interpreter only when its script is packaged too)."""
    if not own.owned(p.exe):
        return False
    if interpreter(p):
        s = script_of(p)
        return s is None or own.owned(s)          # an interactive shell, or a packaged script
    return True


def responsible(pid: int, own: Ownership, reader=read, depth: int = 8) -> Proc | None:
    """The process to judge for what `pid` does (see the module docstring)."""
    p = reader(pid)
    if p is None:
        return None
    first = p
    for _ in range(depth):
        if not trusted(p, own):
            return p
        if p.ppid <= 1:
            break
        parent = reader(p.ppid)
        if parent is None or parent.uid != p.uid or (interpreter(parent) and script_of(parent) is None):
            break                                   # reached an interactive shell or another user
        p = parent
    return first


@dataclass
class Activity:
    writes: int = 0             # files written in user folders
    hot: int = 0                # of them, random-looking (likely encrypted)
    canary: int = 0             # decoy files written
    bad_writes: int = 0         # files written that a signature rule flagged
    paths: list[str] = field(default_factory=list)


def signals(p: Proc, a: Activity, own: Ownership, cpu_frac: float, new_sockets: int, ld_preload: bool,
            age_s: float) -> tuple[float, float, float, list[str]]:
    """(pamp, danger, safe, reasons) for one process over one sampling period."""
    why = []
    pamp = 0.0
    if a.canary:
        pamp += 10; why.append(f"wrote {a.canary} decoy file(s)")
    if a.bad_writes:
        pamp += 5; why.append(f"wrote {a.bad_writes} file(s) flagged by signatures")
    if p.exe.startswith(RISKY_EXE):
        pamp += 2; why.append("runs from a temporary folder")
    if p.deleted:
        pamp += 3; why.append("runs from a deleted file")
    if ld_preload:
        pamp += 2; why.append("LD_PRELOAD injected")
    danger = 0.0
    if a.hot > 3:               # saving a few photos or archives is normal; dozens a second is not
        danger += (a.hot - 3) * 0.8; why.append(f"rewrote {a.hot} file(s) with random-looking data")
    if a.writes:
        danger += a.writes * 0.1
    if cpu_frac > 0.9:          # on its own never enough (simulations, builds, the model), only with other signs
        danger += 1; why.append("CPU flat out")
    if new_sockets > 0:
        danger += min(4, new_sockets * 0.5); why.append(f"{new_sockets} new connection(s)")
    safe = 5.0 if trusted(p, own) else 0.5
    if age_s > 600 and a.writes == 0:
        safe += 2
    return min(10, pamp), min(10, danger), min(10, safe), why


def descendants(pid: int) -> list[int]:
    kids: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                ppid = int(Path(f"/proc/{d}/stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            kids.setdefault(ppid, []).append(int(d))
    out, todo = [], [pid]
    while todo:
        x = todo.pop()
        for k in kids.get(x, []):
            out.append(k)
            todo.append(k)
    return out


def uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except OSError:
        return time.monotonic()
