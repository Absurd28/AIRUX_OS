"""Load controller: every 5 s read memory pressure, temperature and latency; add one active slot
when healthy, halve the active slots when not.

llama-server cannot change its slot count while running, so "active slots" is how many model
requests AIRA lets run at once (orchestrator agents and worker pool both respect it), between 1
and the server's slot count.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

INTERVAL_S = 5.0
PRESSURE_MAX = 10.0      # % of time some task stalled on memory (PSI some avg10)
TEMP_MAX_C = 85.0
LATENCY_MAX_S = 30.0     # median seconds per model request


@dataclass
class Metrics:
    memory_pressure: float | None   # PSI "some avg10" in %
    temperature_c: float | None     # hottest of CPU thermal zones and GPU
    latency_s: float | None         # median of recent model request times


def read_memory_pressure(path: Path = Path("/proc/pressure/memory")) -> float | None:
    try:
        m = re.search(r"some avg10=([\d.]+)", path.read_text())
        return float(m.group(1)) if m else None
    except OSError:
        return None


def read_temperature() -> float | None:
    temps = []
    for z in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            temps.append(int(z.read_text()) / 1000)
        except (OSError, ValueError):
            pass
    exe = shutil.which("nvidia-smi") or ("/usr/lib/wsl/lib/nvidia-smi" if Path("/usr/lib/wsl/lib/nvidia-smi").exists()
                                         else None)
    if exe:
        try:
            out = subprocess.run([exe, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=5).stdout
            temps += [float(x) for x in out.split()]
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return max(temps) if temps else None


@dataclass
class LoadController:
    max_slots: int
    read: Callable[[], Metrics] | None = None
    slots: int = 1
    history: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self._lat: deque[float] = deque(maxlen=20)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.slots = max(1, min(self.slots, self.max_slots))
        if self.read is None:
            self.read = lambda: Metrics(read_memory_pressure(), read_temperature(), self.median_latency())

    def record_latency(self, seconds: float) -> None:
        with self._lock:
            self._lat.append(seconds)

    def median_latency(self) -> float | None:
        with self._lock:
            xs = sorted(self._lat)
        return xs[len(xs) // 2] if xs else None

    @staticmethod
    def healthy(m: Metrics) -> tuple[bool, str]:
        problems = []
        if m.memory_pressure is not None and m.memory_pressure > PRESSURE_MAX:
            problems.append(f"memory pressure {m.memory_pressure:.1f}%")
        if m.temperature_c is not None and m.temperature_c > TEMP_MAX_C:
            problems.append(f"temperature {m.temperature_c:.0f}°C")
        if m.latency_s is not None and m.latency_s > LATENCY_MAX_S:
            problems.append(f"latency {m.latency_s:.1f}s")
        return (not problems), "; ".join(problems) or "healthy"

    def tick(self) -> int:
        m = self.read()
        ok, why = self.healthy(m)
        before = self.slots
        self.slots = min(self.max_slots, self.slots + 1) if ok else max(1, self.slots // 2)
        self.history.append({"t": time.time(), "metrics": m.__dict__, "healthy": ok, "why": why,
                             "slots": f"{before}->{self.slots}"})
        del self.history[:-100]
        return self.slots

    def start(self, interval: float = INTERVAL_S) -> threading.Thread:
        def loop():
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:
                    pass
        th = threading.Thread(target=loop, name="aira-load-controller", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()
