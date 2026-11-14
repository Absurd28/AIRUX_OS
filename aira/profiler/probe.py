"""Measure the machine: RAM, VRAM, CPU cores.

Every number comes from the OS. Memory limits from the process's cgroup are applied,
so running under `systemd-run -p MemoryMax=...` is a real simulated memory cap.
For tests, AIRUX_SIM_RAM_MB / AIRUX_SIM_VRAM_MB override measurements and
AIRUX_NO_GPU=1 hides the GPU.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

MiB = 1024 * 1024
GiB = 1024 * MiB

NVIDIA_SMI_CANDIDATES = ("nvidia-smi", "/usr/lib/wsl/lib/nvidia-smi", "/usr/bin/nvidia-smi")


@dataclass
class Hardware:
    ram_total: int                  # bytes, after cgroup cap
    ram_available: int              # bytes, after cgroup cap
    vram_total: int = 0             # bytes, sum over GPUs
    vram_free: int = 0
    gpu_names: list[str] = field(default_factory=list)
    cpu_cores: int = 1              # physical cores as the OS reports them
    cpu_threads: int = 1
    cgroup_limit: int | None = None  # bytes, if the process is memory-capped
    simulated: list[str] = field(default_factory=list)  # which values were overridden
    notes: list[str] = field(default_factory=list)

    @property
    def total_memory(self) -> int:
        return self.ram_total + self.vram_total

    @property
    def free_memory(self) -> int:
        return self.ram_available + self.vram_free

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_memory"] = self.total_memory
        d["free_memory"] = self.free_memory
        return d


def parse_meminfo(text: str) -> tuple[int, int]:
    """Return (MemTotal, MemAvailable) in bytes from /proc/meminfo text."""
    vals: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].rstrip(":") in ("MemTotal", "MemAvailable"):
            vals[parts[0].rstrip(":")] = int(parts[1]) * 1024
    if "MemTotal" not in vals:
        raise ValueError("MemTotal missing from meminfo")
    return vals["MemTotal"], vals.get("MemAvailable", vals["MemTotal"])


def cgroup_memory(proc_cgroup: str, root: Path = Path("/sys/fs/cgroup")) -> tuple[int | None, int | None]:
    """Return (memory.max, memory.current) for this process's cgroup v2, or None.

    Walks up the hierarchy: the tightest limit on any ancestor applies.
    """
    rel = None
    for line in proc_cgroup.splitlines():
        if line.startswith("0::"):
            rel = line[3:].strip()
    if rel is None:
        return None, None
    limit: int | None = None
    current: int | None = None
    path = root / rel.lstrip("/")
    while True:
        mx = path / "memory.max"
        if mx.exists():
            raw = mx.read_text().strip()
            if raw != "max":
                v = int(raw)
                if limit is None or v < limit:
                    limit = v
                    cur = path / "memory.current"
                    current = int(cur.read_text()) if cur.exists() else None
        if path == root or path.parent == path:
            break
        path = path.parent
    return limit, current


def count_cores(cpuinfo: str) -> tuple[int, int]:
    """Return (physical cores, threads) from /proc/cpuinfo text."""
    cores = set()
    threads = 0
    phys = core = None
    for line in cpuinfo.splitlines() + [""]:
        if line.startswith("processor"):
            threads += 1
        elif line.startswith("physical id"):
            phys = line.split(":", 1)[1].strip()
        elif line.startswith("core id"):
            core = line.split(":", 1)[1].strip()
        elif not line.strip():
            if core is not None:
                cores.add((phys, core))
            phys = core = None
    threads = threads or (os.cpu_count() or 1)
    return (len(cores) or threads), threads


def query_gpus() -> tuple[int, int, list[str], str | None]:
    """Return (vram_total, vram_free, names, error) from nvidia-smi. No GPU -> zeros."""
    exe = next((c for c in NVIDIA_SMI_CANDIDATES if shutil.which(c) or Path(c).is_file()), None)
    if exe is None:
        return 0, 0, [], "nvidia-smi not found"
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return 0, 0, [], f"nvidia-smi failed: {exc}"
    total = free = 0
    names = []
    for line in out.strip().splitlines():
        name, t, f = (p.strip() for p in line.split(","))
        names.append(name)
        total += int(t) * MiB
        free += int(f) * MiB
    return total, free, names, None


def probe(env: dict[str, str] | None = None) -> Hardware:
    env = dict(os.environ if env is None else env)
    ram_total, ram_avail = parse_meminfo(Path("/proc/meminfo").read_text())
    limit, current = cgroup_memory(Path("/proc/self/cgroup").read_text())
    notes: list[str] = []
    if limit is not None and limit < ram_total:
        ram_total = limit
        ram_avail = min(ram_avail, max(0, limit - (current or 0)))
        notes.append(f"cgroup memory cap {limit / GiB:.2f} GiB applied")

    cores, threads = count_cores(Path("/proc/cpuinfo").read_text())
    hw = Hardware(ram_total=ram_total, ram_available=ram_avail, cpu_cores=cores,
                  cpu_threads=threads, cgroup_limit=limit, notes=notes)

    if env.get("AIRUX_NO_GPU") == "1":
        hw.simulated.append("no_gpu")
    else:
        hw.vram_total, hw.vram_free, hw.gpu_names, err = query_gpus()
        if err:
            hw.notes.append(err)

    if "AIRUX_SIM_RAM_MB" in env:
        v = int(env["AIRUX_SIM_RAM_MB"]) * MiB
        hw.ram_total, hw.ram_available = v, min(hw.ram_available, v)
        hw.simulated.append("ram")
    if "AIRUX_SIM_VRAM_MB" in env:
        v = int(env["AIRUX_SIM_VRAM_MB"]) * MiB
        hw.vram_total = hw.vram_free = v
        hw.simulated.append("vram")
    return hw
