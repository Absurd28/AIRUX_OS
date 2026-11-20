"""One verdict per file, from every engine.

  malicious   a signature rule or the blocklist matched: blocked and quarantined automatically
  suspicious  a "suspicious" rule, strong heuristics, Negative Selection together with heuristics, or
              a Clonal Selection memory detector (a variant of a threat seen before): the user decides
  clean       nothing matched, or the user allowed this exact file (by SHA-256)
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

from . import features as F
from .engines import heuristics
from .engines.nsa import NegativeSelection
from .engines.rules import RuleSet, sha256_file

HASH_LIMIT = 64 * 1024 * 1024


@dataclass
class Verdict:
    path: str
    level: str = "clean"          # clean | suspicious | malicious
    threat: str = ""
    engine: str = ""              # blocklist | rules | memory | heuristics | nsa | allowed
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    sha256: str | None = None
    vector: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("vector")
        return d


class Scanner:
    def __init__(self, rules: RuleSet, nsa: NegativeSelection | None = None,
                 allowed: Callable[[str], bool] = lambda sha: False):
        self.rules, self.nsa, self.allowed = rules, nsa, allowed

    def scan(self, path: str, fd: int | None = None, hash_limit: int = HASH_LIMIT) -> Verdict:
        info = F.read(path, fd)
        if info is None:
            return Verdict(path, reasons=["not a readable regular file"])
        sha = sha256_file(path, fd) if info.size <= hash_limit else None
        v = Verdict(path, sha256=sha, vector=info.vector)
        if sha and self.allowed(sha):
            v.engine, v.reasons = "allowed", ["you allowed this file"]
            return v
        hits = self.rules.scan(path, info.data, info.size, sha)
        bad = [h for h in hits if h.severity == "malicious"]
        if bad:
            h = bad[0]
            v.level, v.threat, v.score = "malicious", h.rule, 1.0
            v.engine = "blocklist" if h.description == "file on the blocklist" else "rules"
            v.reasons = [h.description] + [f"matched: {e[:80]}" for e in h.evidence[:2]]
            return v
        executable = info.traits["elf"] or info.traits["script"] or bool(info.mode & 0o111)
        h_score, why = heuristics.score(info) if executable else (0.0, [])
        n_score, memory = self.nsa.score(info.vector) if (self.nsa and executable) else (0.0, "")
        sus = [h for h in hits if h.severity == "suspicious"]
        if memory and n_score >= 0.3:
            v.level, v.threat, v.engine, v.score = "suspicious", f"Variant.{memory}", "memory", n_score
            v.reasons = [f"close to {memory}, a threat Phagos has seen before (clonal memory)"] + why
        elif sus:
            v.level, v.threat, v.engine, v.score = "suspicious", sus[0].rule, "rules", 0.7
            v.reasons = [sus[0].description] + [f"matched: {e[:80]}" for e in sus[0].evidence[:2]]
        elif h_score >= 0.6:
            v.level, v.threat, v.engine, v.score = "suspicious", "Heuristic.Suspicious", "heuristics", h_score
            v.reasons = why
        elif n_score >= 0.4 and h_score >= 0.3:
            v.level, v.threat, v.engine, v.score = "suspicious", "Anomaly.NonSelf", "nsa", (n_score + h_score) / 2
            v.reasons = ["unlike any program this computer normally has (negative selection)"] + why
        else:
            v.score = max(h_score, n_score) * 0.5
        return v


# ---------------------------------------------------------------- what scans cover
SKIP_DIRS = ("/proc", "/sys", "/dev", "/run", "/var/lib/phagos", "/snap", "/var/cache/apt", "/var/lib/docker")


def quick_targets(homes: Iterable[str]) -> list[str]:
    """Where malware usually lands or hooks itself in: temp folders, downloads, autostart, schedules."""
    t = ["/tmp", "/var/tmp", "/dev/shm", "/etc/cron.d", "/etc/cron.daily", "/etc/cron.hourly", "/etc/crontab",
         "/var/spool/cron", "/etc/systemd/system", "/etc/profile.d", "/etc/ld.so.preload", "/etc/rc.local",
         "/etc/xdg/autostart"]
    for h in homes:
        t += [f"{h}/Downloads", f"{h}/Desktop", f"{h}/.config/autostart", f"{h}/.config/systemd/user",
              f"{h}/.local/bin", f"{h}/.bashrc", f"{h}/.profile", f"{h}/.ssh/authorized_keys", f"{h}/.cache/tmp"]
    return t


def running_executables() -> list[str]:
    out = set()
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                continue
            if not exe.endswith(" (deleted)"):
                out.add(exe)
    return sorted(out)


def walk(targets: Iterable[str], max_size: int = 256 * 1024 * 1024) -> Iterator[str]:
    for t in targets:
        p = Path(t)
        if p.is_file():
            yield str(p)
            continue
        if not p.is_dir():
            continue
        for root, dirs, files in os.walk(p, followlinks=False):
            if root.startswith(SKIP_DIRS):
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not os.path.join(root, d).startswith(SKIP_DIRS)]
            for f in files:
                fp = os.path.join(root, f)
                try:
                    st = os.lstat(fp)
                except OSError:
                    continue
                if (st.st_mode & 0o170000) == 0o100000 and st.st_size <= max_size:
                    yield fp
