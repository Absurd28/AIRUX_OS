"""phagosd: Phagos's protection service (runs as root).

Real-time protection    fanotify: files are scanned when written; a program judged malicious or
                        suspicious is not allowed to start
Behaviour monitoring    every 2 s: processes' signals go to the Dendritic Cell Algorithm; an
                        anomalous process is paused (SIGSTOP) and its user decides
Ransomware shield       decoy files in users' folders; writing or removing one is a sure sign
Scans                   quick, full and custom, one at a time, plus a package integrity check
API                     JSON lines on /run/phagos/phagos.sock; what a caller may do depends on its
                        uid (SO_PEERCRED); admin actions are checked with polkit

Response policy (Aryan's choice, like Windows Defender): known malware is blocked and quarantined
automatically; suspicious files are blocked from running or reported, and a process acting
dangerously is paused, until the user chooses kill, quarantine or allow.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import pwd
import random
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, features as F, procmon
from .engines import clonal
from .engines.dca import DendriticCells
from .engines.nsa import NegativeSelection
from .engines.rules import RuleSet
from .quarantine import Quarantine, QuarantineError
from .scanner import Scanner, Verdict, quick_targets, running_executables, walk
from .store import Store

POLKIT_ACTION = "org.airux.phagos.manage"
EXEC_BUDGET_S = 1.0           # a program waits at most this long for its verdict
TICK_S = 2.0
DECOY_FOLDERS = ("Documents", "Pictures", "Desktop", "Downloads")
DECOY_TEXT = ("Quarterly budget review\n\nRevenue and costs by region, to be finalised before the board "
              "meeting. Keep this file: it is used by the system.\n") * 20
HOT_ENTROPY = 7.5
SKIP_WRITES = ("/proc/", "/sys/", "/dev/", "/run/", "/var/lib/phagos/", "/var/log/", "/var/cache/", "/var/lib/apt/",
               "/var/lib/dpkg/", "/var/lib/systemd/", "/var/tmp/systemd-")
SKIP_HOME = ("/.cache/", "/.local/share/Trash/", "/.config/chromium/", "/.config/neox/", "/.local/share/aira/",
             "/.local/state/", "/.mozilla/", "/.local/share/baloo/", "/.config/AIROX Code/", "/.local/share/neox/",
             "/.local/share/airux-snake/", "/.local/share/kactivitymanagerd/")


@dataclass
class Paths:
    state: Path = Path("/var/lib/phagos")
    rules: tuple = (Path("/usr/share/phagos/rules"), Path("/etc/phagos/rules.d"))
    socket: Path = Path("/run/phagos/phagos.sock")
    dpkg_info: str = "/var/lib/dpkg/info"
    homes: tuple | None = None          # None: every user with uid >= 1000 and a home under /home


@dataclass
class Paused:
    event_id: int
    key: str
    pid: int
    uid: int
    exe: str
    script: str | None
    reasons: list[str]
    mcav: float
    ts: float = field(default_factory=time.time)


class Phagos:
    def __init__(self, paths: Paths = Paths(), seed: int | None = None, train: bool = True):
        self.paths = paths
        paths.state.mkdir(parents=True, exist_ok=True)
        os.chmod(paths.state, 0o700)
        self.rules = RuleSet.load(*paths.rules)
        self.store = Store(paths.state / "phagos.db")
        self.quarantine = Quarantine(paths.state / "quarantine")
        self.own = procmon.Ownership(paths.dpkg_info)
        self.nsa: NegativeSelection | None = self._load_nsa()
        self.scanner = Scanner(self.rules, self.nsa, self.store.is_allowed)
        self.dca = DendriticCells(seed=seed)
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.cache: OrderedDict[tuple, Verdict] = OrderedDict()
        self.activity: dict[str, procmon.Activity] = {}
        self.cpu_prev: dict[str, tuple[int, float]] = {}
        self.sock_prev: dict[str, int] = {}
        self.paused: dict[int, Paused] = {}
        self.allowed_behaviour: set[str] = set(self.store.get("allowed_behaviour", []))
        self.subscribers: list[tuple[socket.socket, int]] = []
        self.decoys: dict[str, tuple[int, float]] = {}
        self.scan_job: dict | None = None
        self.realtime = bool(self.store.get("realtime", True))
        self.training = False
        if self.nsa is None and train:
            threading.Thread(target=self.train_self, name="phagos-train", daemon=True).start()

    # ------------------------------------------------------------------ the self model
    def _load_nsa(self) -> NegativeSelection | None:
        p = self.paths.state / "nsa.json"
        if p.exists():
            try:
                return NegativeSelection.from_json(json.loads(p.read_text()))
            except (OSError, ValueError, KeyError):
                pass
        return None

    def train_self(self, limit: int = 2500) -> int:
        """Learn "self" from the programs Debian packages installed, then generate detectors."""
        self.training = True
        try:
            try:
                os.nice(10)
            except OSError:
                pass
            cands = sorted(p for p in self.own.paths
                           if p.startswith(("/usr/bin/", "/usr/sbin/", "/usr/lib/", "/usr/libexec/", "/opt/"))
                           and not p.endswith((".py", ".pyc", ".h", ".txt", ".gz", ".png", ".svg", ".mo", ".json")))
            rng = random.Random(42)
            rng.shuffle(cands)
            vecs = []
            for p in cands:
                if len(vecs) >= limit:
                    break
                try:
                    if not os.path.isfile(p) or os.path.islink(p) or not os.access(p, os.X_OK):
                        continue
                except OSError:
                    continue
                info = F.read(p)
                if info and (info.traits["elf"] or info.traits["script"]):
                    vecs.append(info.vector)
            nsa = NegativeSelection(F.DIM, seed=42)
            nsa.add_self(vecs)
            nsa.train()
            nsa.save(self.paths.state / "nsa.json")
            with self.lock:
                self.nsa = nsa
                self.scanner.nsa = nsa
                self.cache.clear()
            return len(vecs)
        finally:
            self.training = False

    def _save_nsa(self) -> None:
        if self.nsa is not None:
            self.nsa.save(self.paths.state / "nsa.json")

    # ------------------------------------------------------------------ users and decoys
    def homes(self) -> list[tuple[str, int, int]]:
        if self.paths.homes is not None:
            return [(str(h), os.stat(h).st_uid, os.stat(h).st_gid) for h in self.paths.homes if os.path.isdir(h)]
        return [(u.pw_dir, u.pw_uid, u.pw_gid) for u in pwd.getpwall()
                if u.pw_uid >= 1000 and u.pw_dir.startswith("/home/") and os.path.isdir(u.pw_dir)]

    def place_decoys(self) -> int:
        for home, uid, gid in self.homes():
            for d in DECOY_FOLDERS:
                folder = Path(home) / d
                if not folder.is_dir():
                    continue
                p = folder / ".phagos-decoy.docx"
                try:
                    if not p.exists():
                        p.write_text(DECOY_TEXT)
                        os.chown(p, uid, gid)
                        os.chmod(p, 0o600)
                    st = p.stat()
                    self.decoys[str(p)] = (st.st_size, st.st_mtime)
                except OSError:
                    continue
        return len(self.decoys)

    def check_decoys(self) -> list[str]:
        """Decoys removed or changed since last check (by something no write event caught)."""
        hit = []
        for p, (size, mtime) in list(self.decoys.items()):
            try:
                st = os.stat(p)
                if (st.st_size, st.st_mtime) != (size, mtime):
                    hit.append(p)
            except FileNotFoundError:
                hit.append(p)
        return hit

    # ------------------------------------------------------------------ events and notifications
    def publish(self, ev: dict, uid: int | None) -> None:
        line = (json.dumps({"event": ev}, default=str) + "\n").encode()
        dead = []
        for conn, peer_uid in list(self.subscribers):
            if peer_uid in (0, uid) or uid is None:
                try:
                    conn.sendall(line)
                except OSError:
                    dead.append((conn, peer_uid))
        for d in dead:
            if d in self.subscribers:
                self.subscribers.remove(d)

    def record(self, kind: str, verdict: Verdict | None = None, uid: int | None = None, **kw) -> dict:
        data: dict = dict(kind=kind, uid=uid)
        if verdict is not None:
            data.update(level=verdict.level, threat=verdict.threat, path=verdict.path, engine=verdict.engine,
                        detail={"reasons": verdict.reasons, "sha256": verdict.sha256, "score": round(verdict.score, 2)})
        for k, v in kw.items():
            if k == "detail" and isinstance(v, dict) and isinstance(data.get("detail"), dict):
                data["detail"] = {**data["detail"], **v}
            else:
                data[k] = v
        eid = self.store.event(**data)
        data["id"] = eid
        self.publish(data, uid)
        return data

    # ------------------------------------------------------------------ files
    @staticmethod
    def _key(st: os.stat_result) -> tuple:
        return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)

    def verdict(self, path: str, fd: int | None = None, hash_limit: int | None = None) -> Verdict:
        try:
            st = os.fstat(fd) if fd is not None else os.stat(path)
        except OSError:
            return Verdict(path)
        k = self._key(st)
        with self.lock:
            v = self.cache.get(k)
            if v is not None:
                self.cache.move_to_end(k)
                return v
        v = self.scanner.scan(path, fd) if hash_limit is None else self.scanner.scan(path, fd, hash_limit)
        with self.lock:
            self.cache[k] = v
            while len(self.cache) > 50000:
                self.cache.popitem(last=False)
        return v

    def _owner(self, path: str, default: int | None = None) -> int | None:
        try:
            return os.stat(path).st_uid
        except OSError:
            return default

    def contain(self, v: Verdict, uid: int | None, how: str) -> dict:
        """Act on a verdict by policy: quarantine the malicious, report the suspicious, and only note
        an anomaly that Negative Selection alone sees (no popup: builds and new tools are unusual too)."""
        if v.engine == "nsa":
            return self.record("anomaly", v, uid, action="noted")
        if v.level == "malicious" and self.own.unchanged(v.path):
            return self.record("anomaly", v, uid, action="matched a rule, but it is exactly the file its Debian package "
                                                         "installed: left in place")
        if v.level == "malicious":
            try:
                meta = self.quarantine.add(v.path, v.threat, v.level, v.engine)
                action = f"{how}; quarantined"
                if self.nsa is not None and v.vector and (v.vector[2] or v.vector[3] or v.vector[11]):
                    if clonal.mature(self.nsa, v.vector, v.threat, rng=self.rng) is not None:
                        self._save_nsa()
                return self.record("threat", v, uid, action=action, detail={"reasons": v.reasons, "quarantine": meta["id"],
                                                                            "sha256": v.sha256})
            except QuarantineError as exc:
                return self.record("threat", v, uid, action=f"{how}; could not quarantine: {exc}")
        return self.record("suspicious", v, uid, action=how)

    def on_exec(self, path: str, fd: int, pid: int) -> bool:
        """Allow or deny a program about to start. Must return within EXEC_BUDGET_S."""
        if not self.realtime or pid == os.getpid():
            return True
        try:
            packaged = os.fstat(fd).st_uid == 0 and self.own.owned(path)
        except OSError:
            packaged = False
        # a packaged program is checked on its sampled bytes only: hashing a 250 MB browser at every
        # start would cost more than it finds (the weekly integrity check covers replaced binaries)
        v = self.verdict(path, fd, hash_limit=0 if packaged else None)
        if v.level == "clean":
            return True
        if v.engine == "nsa" or (packaged and self.own.unchanged(path, fd)):
            # unusual is not proof, and an unchanged package file is not an intruder: run it, note it
            threading.Thread(target=self.contain, args=(v, None, "ran"), daemon=True).start()
            return True
        uid = (procmon.read(pid) or procmon.Proc(pid, 0, self._owner(path, 0), "", "", [], 0, 0)).uid
        threading.Thread(target=self.contain, args=(v, uid, "blocked from running"), daemon=True).start()
        return False

    def on_written(self, path: str, fd: int, pid: int) -> None:
        if not self.realtime or pid == os.getpid() or not path or path.startswith(SKIP_WRITES):
            return
        in_home = path.startswith("/home/")
        if in_home and any(s in path for s in SKIP_HOME):
            return
        resp = procmon.responsible(pid, self.own) if in_home else None
        if path in self.decoys and resp is not None:
            self.activity.setdefault(resp.key, procmon.Activity()).canary += 1
            self.pause(resp, 1.0, [f"wrote the decoy file {path}"])
            return
        try:
            st = os.fstat(fd)
        except OSError:
            return
        if st.st_size > 64 * 1024 * 1024:
            return
        if in_home and resp is not None and "/." not in path[len("/home/"):]:
            a = self.activity.setdefault(resp.key, procmon.Activity())
            a.writes += 1
            if st.st_size >= 4096:
                try:
                    if F.entropy(os.pread(fd, 65536, 0)) >= HOT_ENTROPY:
                        a.hot += 1
                        a.paths = (a.paths + [path])[-5:]
                except OSError:
                    pass
        v = self.verdict(path, fd)
        if v.level != "clean":
            if resp is not None and v.level == "malicious":
                self.activity.setdefault(resp.key, procmon.Activity()).bad_writes += 1
            self.contain(v, self._owner(path), "found when written")

    # ------------------------------------------------------------------ behaviour (Danger Theory)
    def _allowed(self, p: procmon.Proc) -> bool:
        target = (procmon.script_of(p) if procmon.interpreter(p) else None) or p.exe
        return p.key in self.allowed_behaviour or f"path:{target}" in self.allowed_behaviour

    def tick(self, now: float | None = None, only: set[int] | None = None) -> list[Paused]:
        """One behaviour sample of every active process; pauses the ones the dendritic cells flag.
        `only` limits it to some pids (tests)."""
        now = time.monotonic() if now is None else now
        up = procmon.uptime()
        acts, self.activity = self.activity, {}
        seen, flagged = set(), []
        decoy_hits = self.check_decoys() if self.decoys else []
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            pid = int(d)
            if pid in (1, os.getpid()) or (only is not None and pid not in only):
                continue
            p = procmon.read(pid)
            if p is None or not p.exe or p.name in procmon.PROTECTED:
                continue
            seen.add(p.key)
            prev = self.cpu_prev.get(p.key)
            self.cpu_prev[p.key] = (p.cpu, now)
            cpu_frac = (p.cpu - prev[0]) / procmon.CLK / max(0.1, now - prev[1]) if prev else 0.0
            a = acts.get(p.key, procmon.Activity())
            risky = p.exe.startswith(procmon.RISKY_EXE) or p.deleted
            if not (a.writes or a.canary or a.bad_writes or cpu_frac > 0.5 or risky):
                continue
            if self._allowed(p) or any(x.key == p.key for x in self.paused.values()):
                continue
            n_sock = procmon.sockets(pid)
            new_sock = max(0, n_sock - self.sock_prev.get(p.key, n_sock))
            self.sock_prev[p.key] = n_sock
            age = up - p.start / procmon.CLK
            pamp, danger, safe, why = procmon.signals(p, a, self.own, cpu_frac, new_sock,
                                                      procmon.environ_has(pid, b"LD_PRELOAD"), age)
            if decoy_hits and a.writes and a.writes == max((x.writes for x in acts.values()), default=0):
                pamp, why = 10.0, why + [f"decoy file(s) changed or removed: {', '.join(decoy_hits)}"]
            self.dca.sample(p.key, pamp, danger, safe)
            if self.dca.anomalous(p.key):
                mcav = self.dca.mcav(p.key)[0]
                paused = self.pause(p, mcav, why)
                if paused:
                    flagged.append(paused)
        for gone in set(self.cpu_prev) - seen:
            self.cpu_prev.pop(gone, None)
            self.sock_prev.pop(gone, None)
            self.dca.forget(gone)
        if decoy_hits:
            self.place_decoys()                   # put fresh ones back
        return flagged

    def pause(self, p: procmon.Proc, mcav: float, why: list[str]) -> Paused | None:
        if self._allowed(p) or any(x.key == p.key for x in self.paused.values()):
            return None
        targets = [p.pid] + procmon.descendants(p.pid)
        for t in targets:
            try:
                os.kill(t, signal.SIGSTOP)
            except ProcessLookupError:
                pass
        script = procmon.script_of(p) if procmon.interpreter(p) else None
        ev = self.record("behavior", None, p.uid, level="suspicious", threat="Behavior.Dangerous", path=script or p.exe,
                         pid=p.pid, exe=p.exe, engine="dca", action="paused",
                         detail={"reasons": why, "mcav": round(mcav, 2), "cmdline": p.cmdline[:6]})
        x = Paused(ev["id"], p.key, p.pid, p.uid, p.exe, script, why, mcav)
        self.paused[x.event_id] = x
        return x

    def decide(self, event_id: int, choice: str, peer_uid: int) -> dict:
        x = self.paused.get(event_id)
        if x is None:
            raise ApiError("nothing is waiting for that decision")
        if peer_uid not in (0, x.uid):
            raise ApiError("that process belongs to another user")
        targets = [x.pid] + procmon.descendants(x.pid)
        if choice == "allow":
            for t in targets:
                try:
                    os.kill(t, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            self.allowed_behaviour |= {x.key, f"path:{x.script or x.exe}"}
            self.store.put("allowed_behaviour", sorted(self.allowed_behaviour)[-500:])
            self.dca.forget(x.key)
            action = "allowed by you"
        elif choice in ("kill", "quarantine"):
            for t in targets:
                try:
                    os.kill(t, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            action = "stopped"
            if choice == "quarantine":
                target = x.script or x.exe
                if target and not self.own.owned(target):
                    v = self.scanner.scan(target)
                    v.level, v.threat, v.engine = "malicious", "Behavior.Dangerous", "dca"
                    try:
                        meta = self.quarantine.add(target, v.threat, v.level, v.engine)
                        action = f"stopped; {target} quarantined"
                        if self.nsa is not None and v.vector:
                            clonal.mature(self.nsa, v.vector, "Behavior.Dangerous", rng=self.rng)
                            self._save_nsa()
                        self.record("threat", v, x.uid, action="quarantined after you chose to",
                                    detail={"quarantine": meta["id"], "from_event": event_id})
                    except QuarantineError as exc:
                        action = f"stopped; could not quarantine: {exc}"
                else:
                    action = "stopped (it is a system program, so it was not quarantined)"
            self.dca.forget(x.key)
        else:
            raise ApiError("choose kill, quarantine or allow")
        self.store.set_action(event_id, action)
        del self.paused[event_id]
        out = {"event_id": event_id, "action": action}
        self.publish({"kind": "decided", **out}, x.uid)
        return out

    # ------------------------------------------------------------------ scans
    def start_scan(self, kind: str, path: str | None, uid: int) -> dict:
        with self.lock:
            if self.scan_job and not self.scan_job["done"]:
                raise ApiError("a scan is already running")
            if kind == "quick":
                targets = quick_targets([h for h, u, _ in self.homes() if uid in (0, u)]) + running_executables()
            elif kind == "full":
                targets = ["/"]
            elif kind == "custom" and path:
                real = os.path.realpath(path)
                if not os.path.exists(real):
                    raise ApiError(f"{path} does not exist")
                if uid != 0 and not self._may_read(real, uid):
                    raise ApiError(f"you cannot scan {path}")
                targets = [real]
            else:
                raise ApiError("scan kind must be quick, full or custom (with a path)")
            self.scan_job = {"id": int(time.time()), "kind": kind, "targets": targets[:5], "scanned": 0,
                             "found": [], "current": "", "done": False, "started": time.time(), "finished": None,
                             "uid": uid}
            job = self.scan_job
        threading.Thread(target=self._scan, args=(job, targets), daemon=True).start()
        return {k: v for k, v in job.items() if k != "uid"}

    def _may_read(self, path: str, uid: int) -> bool:
        homes = [h for h, u, _ in self.homes() if u == uid]
        return any(path == h or path.startswith(h + "/") for h in homes) or path.startswith(("/tmp/", "/var/tmp/")) \
            or path in ("/tmp", "/var/tmp")

    def _scan(self, job: dict, targets: list[str]) -> None:
        try:
            for p in walk(targets):
                job["current"] = p
                job["scanned"] += 1
                v = self.verdict(p)
                if v.level != "clean":
                    ev = self.contain(v, self._owner(p, job["uid"]), f"found by a {job['kind']} scan")
                    job["found"].append({"path": p, "level": v.level, "threat": v.threat, "engine": v.engine,
                                         "action": ev.get("action"), "event_id": ev.get("id")})
            if job["kind"] == "full":
                for p in self.integrity():
                    job["found"].append({"path": p, "level": "suspicious", "threat": "Integrity.Modified",
                                         "engine": "dpkg", "action": "reported"})
        finally:
            job["done"], job["finished"], job["current"] = True, time.time(), ""
            self.store.put(f"last_{job['kind']}_scan", {"ts": job["finished"], "scanned": job["scanned"],
                                                        "found": len(job["found"])})
            self.record("scan", None, job["uid"], action=f"{job['kind']} scan finished",
                        detail={"scanned": job["scanned"], "found": len(job["found"])})

    def integrity(self) -> list[str]:
        """Package programs whose contents changed since install (dpkg --verify), excluding config files."""
        try:
            out = spawn(["dpkg", "--verify"], timeout=1800).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        bad = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) == 9 and parts[0][2] == "5" and parts[1] != "c":
                path = parts[-1]
                if path.startswith(("/usr/bin/", "/usr/sbin/", "/usr/lib/", "/bin/", "/sbin/", "/lib/")):
                    bad.append(path)
                    self.record("integrity", None, None, level="suspicious", threat="Integrity.Modified", path=path,
                                engine="dpkg", action="reported", detail={"dpkg": parts[0]})
        return bad

    # ------------------------------------------------------------------ API
    def status(self) -> dict:
        last = {k: self.store.get(f"last_{k}_scan") for k in ("quick", "full", "custom")}
        threats = [e for e in self.store.events(500, ("threat", "suspicious", "behavior"))
                   if e["ts"] > time.time() - 30 * 86400]
        nsa = self.nsa
        return {
            "product": "Phagos", "version": __version__, "realtime": self.realtime,
            "protected": self.realtime and not self.paused,
            "engines": {"rules": len(self.rules.rules), "blocklist": len(self.rules.blocklist),
                        "self_samples": len(nsa.self_points) if nsa else 0,
                        "detectors": len(nsa.detectors) if nsa else 0,
                        "memory_detectors": sum(1 for d in nsa.detectors if d.memory) if nsa else 0,
                        "self_model": "learning" if self.training else ("ready" if nsa else "not learned yet"),
                        "dendritic_cells": "watching"},
            "ransomware_shield": {"decoys": len(self.decoys)},
            "threats_30_days": len(threats), "quarantined": len(self.quarantine.items()),
            "paused": [vars(p) for p in self.paused.values()],
            "last_scans": last, "scan": self.scan_status(),
        }

    def scan_status(self) -> dict | None:
        j = self.scan_job
        return None if j is None else {k: v for k, v in j.items() if k != "uid"}

    def authorized(self, peer: tuple[int, int, int]) -> bool:
        pid, uid, _ = peer
        if uid == 0:
            return True
        try:
            r = spawn(["pkcheck", "--action-id", POLKIT_ACTION, "--process", str(pid), "--allow-user-interaction"],
                      timeout=300)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def api(self, req: dict, peer: tuple[int, int, int]) -> dict:
        m, a = req.get("method"), req.get("params") or {}
        uid = peer[1]
        if m == "status":
            return self.status()
        if m == "scan":
            return self.start_scan(a.get("kind", "quick"), a.get("path"), uid)
        if m == "scan_status":
            return {"scan": self.scan_status()}
        if m == "history":
            ev = self.store.events(int(a.get("limit", 50)))
            return {"events": [e for e in ev if uid == 0 or e["uid"] in (None, uid)]}
        if m == "quarantine_list":
            items = self.quarantine.items()
            return {"items": [i for i in items if uid == 0 or i["uid"] == uid]}
        if m == "quarantine_file":
            path = os.path.realpath(a.get("path", ""))
            if not os.path.isfile(path):
                raise ApiError(f"{a.get('path')} is not a file")
            if self._owner(path) != uid and not self.authorized(peer):
                raise ApiError("not allowed: the file is not yours")
            v = self.scanner.scan(path)
            meta = self.quarantine.add(path, v.threat or "Quarantined.ByUser", v.level, v.engine or "user")
            self.record("threat", v, uid, action="quarantined by you", detail={"quarantine": meta["id"]})
            return {"quarantined": meta}
        if m in ("quarantine_restore", "quarantine_delete", "allow", "unallow", "realtime"):
            if not self.authorized(peer):
                raise ApiError("not allowed: an administrator must approve this")
            if m == "quarantine_restore":
                meta = self.quarantine.restore(a.get("id", ""))
                self.store.allow(meta["sha256"], meta["path"], meta["threat"])
                if self.nsa is not None:
                    info = F.read(meta["path"])
                    if info:
                        self.nsa.tolerate(info.vector)
                        self._save_nsa()
                self.cache.clear()
                self.record("restored", None, uid, path=meta["path"], threat=meta["threat"], action="restored and allowed")
                return {"restored": meta}
            if m == "quarantine_delete":
                self.quarantine.delete(a.get("id", ""))
                return {"deleted": a.get("id")}
            if m == "allow":
                sha = a.get("sha256", "")
                if len(sha) != 64:
                    raise ApiError("allow needs the file's sha256")
                self.store.allow(sha, a.get("path", ""), a.get("threat", ""))
                if self.nsa is not None and a.get("path"):
                    info = F.read(a["path"])
                    if info:
                        self.nsa.tolerate(info.vector)
                        self._save_nsa()
                self.cache.clear()
                return {"allowed": sha}
            if m == "unallow":
                return {"removed": self.store.unallow(a.get("sha256", ""))}
            if m == "realtime":
                self.realtime = bool(a.get("on", True))
                self.store.put("realtime", self.realtime)
                self.record("settings", None, uid, action=f"real-time protection turned {'on' if self.realtime else 'off'}")
                return {"realtime": self.realtime}
        if m == "allowed_list":
            return {"items": self.store.allowed()}
        if m == "decide":
            return self.decide(int(a.get("event_id", -1)), a.get("choice", ""), uid)
        raise ApiError(f"unknown method {m!r}")


class ApiError(Exception):
    pass


def spawn(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run a helper program from phagosd. start_new_session=True keeps Python off its posix_spawn path,
    which holds the interpreter lock while the child starts: the child's start waits for phagosd's
    own fanotify answer, and phagosd could not answer without the lock (a deadlock)."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, start_new_session=True)


# ---------------------------------------------------------------- the service: socket, fanotify, ticks
def peercred(conn: socket.socket) -> tuple[int, int, int]:
    data = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", data)


def serve_socket(ph: Phagos, path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o755)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    os.chmod(path, 0o666)          # anyone may connect; each request is judged by the caller's uid
    srv.listen(16)

    def client(conn: socket.socket) -> None:
        peer = peercred(conn)
        f = conn.makefile("rb")
        try:
            for line in f:
                try:
                    req = json.loads(line)
                except ValueError:
                    conn.sendall(b'{"ok": false, "error": "bad json"}\n')
                    continue
                if req.get("method") == "subscribe":
                    ph.subscribers.append((conn, peer[1]))
                    conn.sendall(b'{"ok": true, "subscribed": true}\n')
                    continue
                try:
                    out = {"ok": True, **ph.api(req, peer)}
                except (ApiError, QuarantineError) as exc:
                    out = {"ok": False, "error": str(exc)}
                except Exception as exc:       # never let one request take the service down
                    out = {"ok": False, "error": f"internal error: {exc}"}
                conn.sendall((json.dumps(out, default=str) + "\n").encode())
        except OSError:
            pass
        finally:
            ph.subscribers[:] = [s for s in ph.subscribers if s[0] is not conn]
            conn.close()

    def accept() -> None:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=client, args=(conn,), daemon=True).start()
    threading.Thread(target=accept, name="phagos-api", daemon=True).start()
    return srv


def watched_mounts() -> list[str]:
    real = {"ext4", "ext3", "ext2", "xfs", "btrfs", "f2fs", "overlay", "vfat", "exfat", "ntfs3", "jfs", "reiserfs"}
    out = []
    for line in Path("/proc/self/mounts").read_text().splitlines():
        dev, mnt, fstype = line.split()[:3]
        mnt = mnt.replace("\\040", " ")
        if fstype in real and not mnt.startswith(("/proc", "/sys", "/run/live/rootfs", "/run/live/medium", "/boot/efi")):
            out.append(mnt)
        elif fstype == "tmpfs" and mnt in ("/tmp", "/dev/shm", "/var/tmp"):
            out.append(mnt)               # where downloaded droppers usually land (Debian 13: /tmp is tmpfs)
    return out or ["/"]


class Realtime:
    """fanotify for Phagos, built so a program can never be left waiting.

    Every permission event is answered exactly once (`pending` is the single source of truth: an
    answer and the closing of its descriptor happen together, under a lock). A watchdog answers
    "allow" for any event still waiting after STALL_S and logs every thread's stack, so a stall is
    both healed and explained. If the reading thread dies, the fanotify descriptor is closed (the
    kernel then allows everything pending) and real-time protection starts again.
    """
    STALL_S = 2.0

    def __init__(self, ph: Phagos):
        from . import fanotify as fa
        self.fa, self.ph = fa, ph
        self.lock = threading.Lock()
        self.pending: dict[int, tuple[object, float]] = {}
        self.pool = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="phagos-exec")
        self.answerers = cf.ThreadPoolExecutor(max_workers=32, thread_name_prefix="phagos-answer")
        self.writes = cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="phagos-write")
        self.fan = None
        self.reader: threading.Thread | None = None
        self.last_dump = 0.0
        self.stalls = 0
        self.backlog = 0
        self.dropped = 0

    def start(self) -> None:
        fa = self.fa
        self.fan = fa.Fanotify()
        for m in watched_mounts():
            try:
                self.fan.mark_mount(m, fa.FAN_CLOSE_WRITE | fa.FAN_OPEN_EXEC_PERM)
            except fa.FanotifyError as exc:
                print(f"phagosd: not watching {m}: {exc}", flush=True)
        print(f"phagosd: real-time protection on {watched_mounts()}", flush=True)
        self.reader = threading.Thread(target=self._read, args=(self.fan,), name="phagos-realtime", daemon=True)
        self.reader.start()

    def answer(self, fd: int, allow: bool) -> bool:
        """Answer one permission event (once; later calls for the same event do nothing)."""
        with self.lock:
            item = self.pending.pop(fd, None)
            if item is None:
                return False
            try:
                os.write(self.fan.fd, self.fa.response(fd, allow))
            except OSError as exc:
                print(f"phagosd: could not answer a program start: {exc}", flush=True)
            finally:
                os.close(fd)
        return True

    def _exec(self, ev) -> None:
        allow = True
        try:
            # the scan reads its own copy of the descriptor: the event's own may be answered and
            # closed (by the watchdog) while a slow scan is still reading
            path, scan_fd = ev.path(), os.dup(ev.fd)
            fut = self.pool.submit(self.ph.on_exec, path, scan_fd, ev.pid)
            fut.add_done_callback(lambda _f, fd=scan_fd: os.close(fd))
            allow = fut.result(timeout=EXEC_BUDGET_S)
        except cf.TimeoutError:
            allow = True                  # never keep a program waiting
        except Exception as exc:
            print(f"phagosd: exec check failed: {exc}", flush=True)
        finally:
            self.answer(ev.fd, allow)

    BACKLOG = 4000                       # write events waiting to be scanned; beyond this, skip

    def _write(self, path: str, pid: int) -> None:
        """Scan a written file by path (the event's descriptor was closed at once, see _read)."""
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            self.ph.on_written(path, fd, pid)
        except FileNotFoundError:
            pass                              # already gone (temporary files)
        except Exception as exc:
            print(f"phagosd: write check failed: {exc}", flush=True)
        finally:
            if fd is not None:
                os.close(fd)
            with self.lock:
                self.backlog -= 1

    def _read(self, fan) -> None:
        while True:
            try:
                events = fan.read()
            except OSError as exc:
                if fan is not self.fan:
                    return                  # replaced by the watchdog
                print(f"phagosd: reading events failed: {exc}", flush=True)
                time.sleep(0.2)
                continue
            for ev in events:
                if ev.fd == self.fa.FAN_NOFD:
                    continue
                if ev.is_perm:
                    with self.lock:
                        self.pending[ev.fd] = (ev, time.monotonic())
                    try:
                        self.answerers.submit(self._exec, ev)
                    except RuntimeError:
                        self.answer(ev.fd, True)
                else:
                    # never hold an event's descriptor in a queue: with many writes (a browser
                    # starting) the open descriptors reach the limit, the kernel can then hand
                    # Phagos no events at all, and every program start waits for an answer
                    # that cannot come. Keep the path, close the descriptor now.
                    path = ev.path()
                    os.close(ev.fd)
                    if not path or path.startswith(SKIP_WRITES) or                             (path.startswith("/home/") and any(x in path for x in SKIP_HOME)):
                        continue
                    with self.lock:
                        if self.backlog >= self.BACKLOG:
                            self.dropped += 1
                            continue
                        self.backlog += 1
                    self.writes.submit(self._write, path, ev.pid)

    def watchdog(self) -> None:
        import faulthandler
        while True:
            time.sleep(0.5)
            now = time.monotonic()
            with self.lock:
                late = [fd for fd, (_, t) in self.pending.items() if now - t > self.STALL_S]
            for fd in late:
                if self.answer(fd, True):
                    self.stalls += 1
            if late and now - self.last_dump > 60:
                self.last_dump = now
                print(f"phagosd: {len(late)} program start(s) waited over {self.STALL_S}s; allowed them. Threads:",
                      flush=True)
                faulthandler.dump_traceback(file=sys.stdout, all_threads=True)
                sys.stdout.flush()
            if self.reader is not None and not self.reader.is_alive():
                print("phagosd: real-time thread stopped; restarting it", flush=True)
                old = self.fan
                with self.lock:
                    for fd in list(self.pending):
                        os.close(fd)
                    self.pending.clear()
                try:
                    old.close()                 # the kernel allows anything still waiting
                except OSError:
                    pass
                self.start()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="phagosd")
    ap.add_argument("--train-only", action="store_true", help="learn the self model and exit (image build)")
    args = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("phagosd must run as root", file=sys.stderr)
        return 1
    ph = Phagos(train=not args.train_only)
    if args.train_only:
        n = ph.train_self()
        print(f"phagos: learned self from {n} programs; {len(ph.nsa.detectors)} detectors")
        return 0
    ph.place_decoys()
    serve_socket(ph, ph.paths.socket)
    import faulthandler
    import resource
    faulthandler.register(signal.SIGUSR1, all_threads=True)      # kill -USR1: every thread's stack
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)      # one descriptor per event in flight
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    rt = Realtime(ph)
    rt.start()
    threading.Thread(target=rt.watchdog, name="phagos-watchdog", daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    last_prune = last_decoys = time.time()
    print(f"phagosd {__version__}: {len(ph.rules.rules)} rules, self model "
          f"{'ready' if ph.nsa else 'learning'}, {len(ph.decoys)} decoys", flush=True)
    while True:
        try:
            for p in ph.tick():
                print(f"phagosd: paused {p.exe} (pid {p.pid}): {'; '.join(p.reasons)}", flush=True)
        except Exception as exc:
            print(f"phagosd: behaviour check failed: {exc}", flush=True)
        if time.time() - last_prune > 86400:
            ph.store.prune()
            last_prune = time.time()
        if time.time() - last_decoys > 60:      # users and their folders can appear after boot
            ph.place_decoys()
            last_decoys = time.time()
        time.sleep(TICK_S)


if __name__ == "__main__":
    sys.exit(main())
