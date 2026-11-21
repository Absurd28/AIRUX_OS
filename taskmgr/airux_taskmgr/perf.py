"""Performance: CPU, memory, disks, network and GPUs, with a 60-sample history for the graphs."""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

HISTORY = 60


def _read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def cpu_times(proc: Path) -> dict[str, tuple[int, int]]:
    """'cpu', 'cpu0', ... -> (busy, total) ticks."""
    out = {}
    for line in _read(proc / "stat").splitlines():
        if not line.startswith("cpu"):
            break
        f = line.split()
        v = [int(x) for x in f[1:9]]
        idle = v[3] + v[4]          # idle + iowait
        out[f[0]] = (sum(v) - idle, sum(v))
    return out


def meminfo(proc: Path) -> dict[str, int]:
    out = {}
    for line in _read(proc / "meminfo").splitlines():
        k, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[k] = int(parts[0]) * (1024 if len(parts) > 1 else 1)
    return out


def cpu_model(proc: Path) -> str:
    for line in _read(proc / "cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return "CPU"


def disks(sys_root: Path) -> list[str]:
    out = []
    for d in sorted((sys_root / "block").glob("*")):
        if re.match(r"(loop|ram|zram|dm-|sr|md)", d.name):
            continue
        out.append(d.name)
    return out


def diskstats(proc: Path) -> dict[str, tuple[int, int, int]]:
    """name -> (sectors read, sectors written, ms spent doing I/O)."""
    out = {}
    for line in _read(proc / "diskstats").splitlines():
        f = line.split()
        if len(f) >= 14:
            out[f[2]] = (int(f[5]), int(f[9]), int(f[12]))
    return out


def netdev(proc: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for line in _read(proc / "net" / "dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        f = rest.split()
        if len(f) >= 9:
            out[name.strip()] = (int(f[0]), int(f[8]))
    return out


def nvidia_gpus() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    gpus = []
    for line in out.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 7:
            continue
        num = lambda s: float(s) if re.fullmatch(r"[0-9.]+", s) else None  # noqa: E731
        gpus.append({"id": f"nvidia{f[0]}", "name": f[1], "vendor": "NVIDIA", "util": num(f[2]),
                     "mem_used": int(num(f[3]) or 0) * 1024 * 1024, "mem_total": int(num(f[4]) or 0) * 1024 * 1024,
                     "temp": num(f[5]), "power": num(f[6])})
    return gpus


def drm_gpus(sys_root: Path) -> list[dict]:
    """AMD (and other DRM drivers that publish gpu_busy_percent / VRAM counters) from sysfs."""
    gpus = []
    for card in sorted((sys_root / "class" / "drm").glob("card[0-9]")):
        dev = card / "device"
        drv = Path(os.path.realpath(dev / "driver")).name if (dev / "driver").exists() else ""
        if drv == "nvidia":
            continue                         # nvidia-smi covers it
        busy = _read(dev / "gpu_busy_percent").strip()
        used = _read(dev / "mem_info_vram_used").strip()
        total = _read(dev / "mem_info_vram_total").strip()
        vendor = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA", "0x1af4": "Virtio", "0x1234": "QEMU"}.get(
            _read(dev / "vendor").strip(), drv or "GPU")
        gpus.append({"id": card.name, "name": f"{vendor} ({drv})" if drv else vendor, "vendor": vendor,
                     "util": float(busy) if busy.isdigit() else None,
                     "mem_used": int(used) if used.isdigit() else None, "mem_total": int(total) if total.isdigit() else None,
                     "temp": None, "power": None})
    return gpus


class Perf:
    def __init__(self, proc_root: str = "/proc", sys_root: str = "/sys", gpu_every: float = 2.0):
        self.proc, self.sys = Path(proc_root), Path(sys_root)
        self.prev_cpu: dict[str, tuple[int, int]] = {}
        self.prev_disk: dict[str, tuple[int, int, int]] = {}
        self.prev_net: dict[str, tuple[int, int]] = {}
        self.prev_t = 0.0
        self.hist: dict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=HISTORY))
        self.model = cpu_model(self.proc)
        self.gpu_every, self._gpu_t, self._gpus = gpu_every, 0.0, []

    def _push(self, key: str, value) -> list:
        self.hist[key].append(value)
        return list(self.hist[key])

    def sample(self) -> dict:
        now = time.monotonic()
        dt = now - self.prev_t if self.prev_t else 0
        cpu = cpu_times(self.proc)
        pct = {}
        for k, (busy, total) in cpu.items():
            ob, ot = self.prev_cpu.get(k, (busy, total))
            pct[k] = round(100.0 * (busy - ob) / (total - ot), 1) if total > ot else 0.0
        mi = meminfo(self.proc)
        mem_total, mem_avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
        freqs = []
        for f in sorted(self.sys.glob("devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")):
            v = _read(f).strip()
            if v.isdigit():
                freqs.append(int(v) // 1000)
        load = _read(self.proc / "loadavg").split()
        uptime = _read(self.proc / "uptime").split()
        out = {
            "time": time.time(),
            "cpu": {"model": self.model, "util": pct.get("cpu", 0.0), "history": self._push("cpu", pct.get("cpu", 0.0)),
                    "cores": [pct[k] for k in sorted((k for k in pct if k != "cpu"), key=lambda s: int(s[3:]))],
                    "mhz": round(sum(freqs) / len(freqs)) if freqs else None, "count": len(pct) - 1 if pct else 0,
                    "load": [float(x) for x in load[:3]] if len(load) >= 3 else [],
                    "procs": int(load[3].split("/")[1]) if len(load) >= 4 and "/" in load[3] else None,
                    "uptime": float(uptime[0]) if uptime else None},
            "memory": {"total": mem_total, "used": mem_total - mem_avail, "available": mem_avail,
                       "cached": mi.get("Cached", 0) + mi.get("Buffers", 0), "swap_total": mi.get("SwapTotal", 0),
                       "swap_used": mi.get("SwapTotal", 0) - mi.get("SwapFree", 0),
                       "history": self._push("mem", round(100.0 * (mem_total - mem_avail) / mem_total, 1) if mem_total else 0)},
            "disks": [], "network": [], "gpus": [],
        }
        ds = diskstats(self.proc)
        for name in disks(self.sys):
            if name not in ds:
                continue
            r, w, io_ms = ds[name]
            pr, pw, pio = self.prev_disk.get(name, (r, w, io_ms))
            rbps = (r - pr) * 512 / dt if dt else 0.0
            wbps = (w - pw) * 512 / dt if dt else 0.0
            active = min(100.0, (io_ms - pio) / (dt * 10)) if dt else 0.0
            size = _read(self.sys / "block" / name / "size").strip()
            out["disks"].append({"name": name, "model": _read(self.sys / "block" / name / "device" / "model").strip(),
                                 "size": int(size) * 512 if size.isdigit() else None,
                                 "ssd": _read(self.sys / "block" / name / "queue" / "rotational").strip() == "0",
                                 "read_bps": rbps, "write_bps": wbps, "active": round(active, 1),
                                 "history": self._push(f"disk:{name}", round(active, 1))})
        self.prev_disk = ds
        nd = netdev(self.proc)
        for name, (rx, tx) in sorted(nd.items()):
            if name == "lo":
                continue
            prx, ptx = self.prev_net.get(name, (rx, tx))
            rbps = (rx - prx) / dt if dt else 0.0
            tbps = (tx - ptx) / dt if dt else 0.0
            kind = "Wi-Fi" if (self.sys / "class" / "net" / name / "wireless").exists() else (
                "Ethernet" if (self.sys / "class" / "net" / name / "device").exists() else "Virtual")
            out["network"].append({"name": name, "kind": kind, "rx_bps": rbps, "tx_bps": tbps,
                                   "up": _read(self.sys / "class" / "net" / name / "operstate").strip() == "up",
                                   "history": self._push(f"net:{name}", round((rbps + tbps) * 8 / 1000, 1))})
        self.prev_net = nd
        if now - self._gpu_t >= self.gpu_every or not self._gpu_t:
            self._gpus, self._gpu_t = nvidia_gpus() + drm_gpus(self.sys), now
        for g in self._gpus:
            out["gpus"].append({**g, "history": self._push(f"gpu:{g['id']}", g["util"] or 0)})
        self.prev_cpu, self.prev_t = cpu, now
        return out


def users() -> list[dict]:
    """Logged-in sessions (loginctl)."""
    try:
        r = subprocess.run(["loginctl", "list-sessions", "--output=json"], capture_output=True, text=True, timeout=5)
        rows = json.loads(r.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    return [{"session": s.get("session"), "uid": s.get("uid"), "user": s.get("user"), "seat": s.get("seat") or "",
             "tty": s.get("tty") or "", "state": s.get("state", ""), "idle": s.get("idle", False)} for s in rows]
