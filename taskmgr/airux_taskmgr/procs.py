"""Processes from /proc, grouped like Windows Task Manager: Apps, Background processes, System.

- Apps: processes in a systemd `app-*.scope` / `app-*.service` cgroup of the user's session (KDE
  Plasma and systemd start every launched application in one). The unit name carries the desktop
  file id, which gives the app's name and icon.
- Background processes: the user's other processes (session services, daemons).
- System processes: other users' and root's processes. Kernel threads are left out.

CPU % is a share of the whole machine (all cores = 100 %), as in Windows. Disk rates come from
/proc/<pid>/io (readable for the user's own processes). GPU per process comes from `nvidia-smi
pmon` when an NVIDIA GPU is present.
"""
from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PAGE = os.sysconf("SC_PAGE_SIZE")
TICKS = os.sysconf("SC_CLK_TCK")
APP_DIRS = ("~/.local/share/applications", "/usr/local/share/applications", "/usr/share/applications",
            "/var/lib/flatpak/exports/share/applications", "~/.local/share/flatpak/exports/share/applications",
            "/etc/xdg/autostart")


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    cmdline: str
    uid: int
    user: str
    state: str
    threads: int
    nice: int
    start: int                  # start time in clock ticks after boot (pid + start identify a process)
    memory: int                 # resident bytes
    cpu: float = 0.0            # % of the whole machine
    read_bps: float = 0.0
    write_bps: float = 0.0
    gpu: float | None = None    # % (NVIDIA)
    gpu_mem: int | None = None  # MiB (NVIDIA)
    cgroup: str = ""
    exe: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Group:
    key: str                    # the cgroup path, or "pid:<n>" for a lone process
    kind: str                   # app | background | system
    name: str
    icon: str
    procs: list[Proc] = field(default_factory=list)

    def to_dict(self) -> dict:
        tot = lambda k: sum((getattr(p, k) or 0) for p in self.procs)  # noqa: E731
        gpus = [p.gpu for p in self.procs if p.gpu is not None]
        return {"key": self.key, "kind": self.kind, "name": self.name, "icon": self.icon,
                "cpu": round(tot("cpu"), 1), "memory": tot("memory"), "read_bps": tot("read_bps"),
                "write_bps": tot("write_bps"), "gpu": round(sum(gpus), 1) if gpus else None,
                "count": len(self.procs), "pids": [p.pid for p in self.procs],
                "procs": [p.to_dict() for p in sorted(self.procs, key=lambda p: -p.cpu)]}


def _unescape(s: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)


def app_id_from_unit(unit: str) -> str | None:
    """Desktop file id from a systemd app unit name (XDG/systemd naming:
    app[-<launcher>]-<ApplicationID>[@<random>].service, app[-<launcher>]-<ApplicationID>-<random>.scope;
    dashes inside the launcher and id are escaped as \\x2d)."""
    m = re.fullmatch(r"app-(.+)\.(scope|service)", unit)
    if not m:
        return None
    stem, kind = m.group(1), m.group(2)
    if kind == "service":
        stem = stem.split("@", 1)[0]
    parts = stem.split("-")
    if kind == "scope" and len(parts) > 1:
        parts = parts[:-1]           # the random suffix
    if not parts or not parts[-1]:
        return None
    return _unescape(parts[-1])


class DesktopNames:
    """Desktop file id -> (Name, Icon), read once per id."""

    def __init__(self, dirs: tuple[str, ...] = APP_DIRS):
        self.dirs = [Path(os.path.expanduser(d)) for d in dirs]
        self.cache: dict[str, tuple[str, str] | None] = {}

    def lookup(self, app_id: str) -> tuple[str, str] | None:
        if app_id in self.cache:
            return self.cache[app_id]
        found = None
        for d in self.dirs:
            f = d / f"{app_id}.desktop"
            if f.is_file():
                found = parse_desktop_name(f)
                if found:
                    break
        self.cache[app_id] = found
        return found


def parse_desktop_name(path: Path) -> tuple[str, str] | None:
    name = icon = ""
    try:
        section = ""
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line
                continue
            if section != "[Desktop Entry]":
                continue
            if line.startswith("Name=") and not name:
                name = line[5:].strip()
            elif line.startswith("Icon=") and not icon:
                icon = line[5:].strip()
    except OSError:
        return None
    return (name, icon) if name else None


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def parse_stat(text: str) -> dict | None:
    """/proc/<pid>/stat: the name is in parentheses and may itself contain spaces or ')'."""
    try:
        l, r = text.index("("), text.rindex(")")
    except ValueError:
        return None
    rest = text[r + 2:].split()
    if len(rest) < 20:
        return None
    return {"comm": text[l + 1:r], "state": rest[0], "ppid": int(rest[1]), "utime": int(rest[11]),
            "stime": int(rest[12]), "nice": int(rest[16]), "threads": int(rest[17]), "start": int(rest[19]),
            "rss_pages": int(rest[21]) if len(rest) > 21 else 0}


def total_ticks(proc: Path) -> int:
    line = _read(proc / "stat").split("\n", 1)[0].split()
    if not line or line[0] != "cpu":
        return 0
    v = [int(x) for x in line[1:]]
    return sum(v[:8])                # guest time is already in user


class GpuMonitor:
    """NVIDIA per-process GPU use from `nvidia-smi pmon` (in the background, every few seconds)."""

    def __init__(self, interval: float = 4.0):
        self.exe = shutil.which("nvidia-smi")
        self.interval = interval
        self.data: dict[int, tuple[float, int]] = {}
        self._started = False

    def start(self) -> None:
        if self.exe and not self._started:
            self._started = True
            threading.Thread(target=self._loop, name="taskmgr-gpu", daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                out = subprocess.run([self.exe, "pmon", "-c", "1", "-s", "um"], capture_output=True, text=True,
                                     timeout=15).stdout
                self.data = parse_pmon(out)
            except (OSError, subprocess.TimeoutExpired):
                self.data = {}
            time.sleep(self.interval)


def parse_pmon(text: str) -> dict[int, tuple[float, int]]:
    """pid -> (sm %, framebuffer MiB). The header names the columns (they differ between drivers)."""
    cols: list[str] = []
    out: dict[int, tuple[float, int]] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            if not cols:
                cols = line[1:].split()
            continue
        f = line.split()
        if not cols or len(f) < len(cols) - 1:
            continue
        rec = dict(zip(cols, f))
        try:
            pid = int(rec.get("pid", "-"))
        except ValueError:
            continue
        num = lambda k: float(rec[k]) if rec.get(k, "-") not in ("-", "") else 0.0  # noqa: E731
        sm, fb = num("sm"), int(num("fb"))
        prev = out.get(pid, (0.0, 0))
        out[pid] = (prev[0] + sm, prev[1] + fb)
    return out


class Sampler:
    """Takes process snapshots; rates (CPU, disk) are deltas between two snapshots."""

    def __init__(self, proc_root: str = "/proc", uid: int | None = None, names: DesktopNames | None = None,
                 gpu: GpuMonitor | None = None):
        self.proc = Path(proc_root)
        self.uid = os.getuid() if uid is None else uid
        self.names = names or DesktopNames()
        self.gpu = gpu
        self.prev: dict[tuple[int, int], tuple[int, int, int]] = {}   # (pid, start) -> (ticks, read, write)
        self.prev_total = 0
        self.prev_time = 0.0
        self.ncpu = os.cpu_count() or 1
        self._users: dict[int, str] = {}
        self._lock = threading.Lock()

    def _user(self, uid: int) -> str:
        if uid not in self._users:
            try:
                self._users[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                self._users[uid] = str(uid)
        return self._users[uid]

    def snapshot(self) -> list[Proc]:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> list[Proc]:
        now = time.monotonic()
        total = total_ticks(self.proc)
        dt_ticks = max(total - self.prev_total, 1) if self.prev_total else 0
        dt_s = max(now - self.prev_time, 1e-3) if self.prev_time else 0
        gpu = self.gpu.data if self.gpu else {}
        out: list[Proc] = []
        seen: dict[tuple[int, int], tuple[int, int, int]] = {}
        for d in self.proc.iterdir():
            if not d.name.isdigit():
                continue
            pid = int(d.name)
            st = parse_stat(_read(d / "stat"))
            if st is None:
                continue
            cmd = _read(d / "cmdline").replace("\0", " ").strip()
            if st["ppid"] == 2 or pid == 2 or (not cmd and st["ppid"] == 0 and pid != 1):
                continue                    # kernel threads
            uid = self._uid(d)
            io = _parse_io(_read(d / "io"))
            ticks = st["utime"] + st["stime"]
            key = (pid, st["start"])
            seen[key] = (ticks, io[0], io[1])
            p = Proc(pid=pid, ppid=st["ppid"], name=_display_name(st["comm"], cmd), cmdline=cmd[:600], uid=uid,
                     user=self._user(uid), state=st["state"], threads=st["threads"], nice=st["nice"],
                     start=st["start"], memory=self._rss(d, st), cgroup=_cgroup(_read(d / "cgroup")))
            old = self.prev.get(key)
            if old and dt_ticks:
                p.cpu = round(max(ticks - old[0], 0) * 100.0 / dt_ticks, 1)
                p.read_bps = max(io[0] - old[1], 0) / dt_s
                p.write_bps = max(io[1] - old[2], 0) / dt_s
            if pid in gpu:
                p.gpu, p.gpu_mem = gpu[pid]
            try:
                p.exe = os.readlink(d / "exe")
            except OSError:
                pass
            out.append(p)
        self.prev, self.prev_total, self.prev_time = seen, total, now
        return out

    def _uid(self, d: Path) -> int:
        for line in _read(d / "status").splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
        try:
            return d.stat().st_uid
        except OSError:
            return -1

    def _rss(self, d: Path, st: dict) -> int:
        statm = _read(d / "statm").split()
        if len(statm) >= 3:
            # resident minus shared file pages: close to Windows' "memory (private working set)"
            return max(int(statm[1]) - int(statm[2]), 0) * PAGE
        return st["rss_pages"] * PAGE

    def groups(self, procs: list[Proc] | None = None) -> list[Group]:
        procs = self.snapshot() if procs is None else procs
        groups: dict[str, Group] = {}
        for p in procs:
            unit = p.cgroup.rsplit("/", 1)[-1]
            app_id = app_id_from_unit(unit) if p.uid == self.uid and "/app.slice/" in f"/{p.cgroup}/" else None
            if app_id:
                g = groups.get(p.cgroup)
                if g is None:
                    name, icon = self.names.lookup(app_id) or (_pretty(app_id), "")
                    g = groups[p.cgroup] = Group(p.cgroup, "app", name, icon)
                g.procs.append(p)
            else:
                kind = "background" if p.uid == self.uid else "system"
                groups[f"pid:{p.pid}"] = Group(f"pid:{p.pid}", kind, p.name, "", [p])
        return sorted(groups.values(), key=lambda g: ({"app": 0, "background": 1, "system": 2}[g.kind], g.name.lower()))


def _pretty(app_id: str) -> str:
    last = app_id.rsplit(".", 1)[-1] if "." in app_id else app_id
    return last.replace("-", " ").replace("_", " ").title() or app_id


def _display_name(comm: str, cmd: str) -> str:
    if cmd:
        first = cmd.split(" ", 1)[0]
        base = os.path.basename(first)
        if re.fullmatch(r"(python[0-9.]*|sh|bash|dash|perl|node)", base) and " " in cmd:
            args = [a for a in cmd.split(" ")[1:] if a]
            if args and args[0] in ("-c", "-e"):
                return base                       # inline code: no script name
            if len(args) > 1 and args[0] == "-m":
                return f"{args[1]} ({base})"      # python -m module
            arg = next((a for a in args if not a.startswith("-")), "")
            if arg:
                return f"{os.path.basename(arg)} ({base})"
        if base:
            return base[:64]
    return comm


def _cgroup(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:].strip()
    return ""


def _parse_io(text: str) -> tuple[int, int]:
    r = w = 0
    for line in text.splitlines():
        if line.startswith("read_bytes:"):
            r = int(line.split()[1])
        elif line.startswith("write_bytes:"):
            w = int(line.split()[1])
    return r, w


def find(procs: list[Proc], pid: int) -> Proc | None:
    return next((p for p in procs if p.pid == pid), None)


def alive(pid: int, start: int, proc_root: str = "/proc") -> bool:
    """Is this exact process (same pid and start time) still running? Guards against pid reuse."""
    st = parse_stat(_read(Path(proc_root) / str(pid) / "stat"))
    return bool(st) and st["start"] == start and st["state"] not in ("Z", "X")
