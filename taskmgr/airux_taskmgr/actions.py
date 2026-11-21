"""Task Manager actions, all through AIRA's permission broker (aira.broker.Broker).

Risk is computed in code from the arguments:
  end task / end an app ............ stop (+ root for another user's process)
  priority: lower (nicer) .......... write;  raise ...................... root
  service start .................... write (user) / root (system)
  service stop, restart ............ stop (+ root for a system service)
  startup app on/off ............... write (a new user override) / overwrite (changes your own file)
Anything in aira.tools.base.CONFIRM runs only after the confirm callback says yes. Every request,
decision and result is logged through the broker's sink.

Confirmations (used by the desktop app and by AIROX Connect): `Confirmations.ask` publishes a
`confirm` event and waits for `answer`; unanswered requests are denied after TIMEOUT seconds.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from typing import Callable

from aira.broker import Broker
from aira.tools.base import CONFIRM, I, Registry, Risk, S, Tool, ToolError

from . import procs as P
from . import services as SV
from . import startup as ST
from .perf import Perf, users

TIMEOUT = 120.0
CALLER = "taskmgr"


class Confirmations:
    def __init__(self, publish: Callable[[dict], None], timeout: float = TIMEOUT):
        self.publish, self.timeout = publish, timeout
        self.pending: dict[str, dict] = {}
        self.lock = threading.Lock()

    def ask(self, ev: dict) -> bool:
        cid = uuid.uuid4().hex[:12]
        ev = {"event": "confirm", "id": cid, "timeout": self.timeout, **ev}
        done = threading.Event()
        with self.lock:
            self.pending[cid] = {"event": ev, "done": done, "answer": False}
        self.publish(ev)
        answered = done.wait(self.timeout)
        with self.lock:
            p = self.pending.pop(cid, None)
        yes = bool(answered and p and p["answer"])
        self.publish({"event": "confirm_done", "id": cid, "yes": yes,
                      "why": "answered" if answered else "timed out (denied)"})
        return yes

    def answer(self, cid: str, yes: bool) -> bool:
        with self.lock:
            p = self.pending.get(cid)
            if p is None:
                return False
            p["answer"] = bool(yes)
            p["done"].set()
        return True

    def events(self) -> list[dict]:
        with self.lock:
            return [p["event"] for p in self.pending.values()]


class TaskManager:
    """The shared backend: a sampler thread (every `interval` s) and the broker-gated actions."""

    def __init__(self, confirm: Callable[[dict], bool], sink: Callable[[dict], None] | None = None,
                 log_path=None, interval: float = 1.0, proc_root: str = "/proc", sys_root: str = "/sys"):
        self.proc_root = proc_root
        self.gpu = P.GpuMonitor()
        self.sampler = P.Sampler(proc_root, gpu=self.gpu)
        self.perf = Perf(proc_root, sys_root)
        self.interval = interval
        self.latest: dict = {"groups": [], "perf": None, "time": 0}
        self._confirm_ui = confirm
        self.registry = Registry(agents=(CALLER,))
        register(self.registry, self)
        self.broker = Broker(self.registry, log_path, confirm=self._confirm, sink=sink)
        self._running = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ sampling
    def start(self) -> "TaskManager":
        if not self._running:
            self._running = True
            self.gpu.start()
            self.tick()
            threading.Thread(target=self._loop, name="taskmgr-sampler", daemon=True).start()
        return self

    def _loop(self) -> None:
        while self._running:
            time.sleep(self.interval)
            try:
                self.tick()
            except Exception:       # a vanished /proc entry must never stop the sampler
                pass

    def tick(self) -> dict:
        procs = self.sampler.snapshot()
        groups = self.sampler.groups(procs)
        perf = self.perf.sample()
        with self._lock:
            self.latest = {"time": time.time(), "procs": procs, "groups": [g.to_dict() for g in groups], "perf": perf}
        return self.latest

    def snapshot(self) -> dict:
        with self._lock:
            return self.latest

    def processes(self) -> dict:
        s = self.snapshot()
        return {"time": s["time"], "groups": s["groups"]}

    def performance(self) -> dict:
        return self.snapshot()["perf"] or {}

    def services(self) -> dict:
        return {"system": SV.listing("system"), "user": SV.listing("user")}

    def startup_apps(self) -> dict:
        return {"apps": ST.entries()}

    def users(self) -> dict:
        s = self.snapshot()
        per: dict[str, dict] = {}
        for p in s.get("procs", []):
            u = per.setdefault(p.user, {"cpu": 0.0, "memory": 0, "count": 0})
            u["cpu"] += p.cpu
            u["memory"] += p.memory
            u["count"] += 1
        return {"sessions": users(), "usage": {k: {**v, "cpu": round(v["cpu"], 1)} for k, v in per.items()}}

    def find(self, pid: int) -> P.Proc | None:
        return P.find(self.snapshot().get("procs", []), pid)

    # ------------------------------------------------------------------ actions
    def run(self, tool: str, args: dict, caller: str = CALLER) -> dict:
        decision, result = self.broker.execute(caller, tool, args)
        return {"ok": result.ok, "allowed": decision.allowed, "decision": decision.reason,
                "risks": sorted(r.value for r in decision.risks), "data": result.data, "error": result.error}

    def _confirm(self, agent: str, tool: str, args: dict, risks: set[Risk]) -> bool:
        return self._confirm_ui({"agent": agent, "tool": tool, "args": args, "risks": sorted(r.value for r in risks),
                                 "summary": describe(self, tool, args)})


# ---------------------------------------------------------------------- the tools
def _own(p: P.Proc) -> bool:
    return p.uid == os.getuid()


def _proc(tm: TaskManager, pid: int, start: int) -> P.Proc:
    p = tm.find(pid)
    if p is None or p.start != start or not P.alive(pid, start, tm.proc_root):
        raise ToolError(f"process {pid} is not running any more")
    return p


def _wait_gone(pairs: list[tuple[int, int]], proc_root: str, seconds: float = 4.0) -> list[int]:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        left = [pid for pid, st in pairs if P.alive(pid, st, proc_root)]
        if not left:
            return []
        time.sleep(0.1)
    return [pid for pid, st in pairs if P.alive(pid, st, proc_root)]


def _signal(p: P.Proc, sig: int) -> None:
    if _own(p):
        os.kill(p.pid, sig)
    else:
        r = subprocess.run(["pkexec", "kill", f"-{signal.Signals(sig).name[3:]}", str(p.pid)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise ToolError("an administrator did not allow it" if r.returncode in (126, 127)
                            else (r.stderr or r.stdout).strip() or f"kill exit {r.returncode}")


def make_handlers(tm: TaskManager) -> dict[str, Callable]:
    def end_task(pid: int, start: int, force: bool = False) -> dict:
        p = _proc(tm, pid, start)
        if pid == os.getpid():
            raise ToolError("that is the Task Manager itself")
        _signal(p, signal.SIGKILL if force else signal.SIGTERM)
        left = _wait_gone([(pid, start)], tm.proc_root)
        return {"name": p.name, "pid": pid, "verified": not left,
                "detail": f"{p.name} ({pid}) has ended" if not left else
                          f"{p.name} ({pid}) is still running; it may be asking to save. End it with force?"}

    def end_app(key: str, force: bool = False) -> dict:
        g = next((g for g in tm.snapshot()["groups"] if g["key"] == key), None)
        if g is None:
            raise ToolError("that app is not running any more")
        pairs = [(p["pid"], p["start"]) for p in g["procs"]]
        for pid, st in pairs:
            p = tm.find(pid)
            if p and P.alive(pid, st, tm.proc_root):
                try:
                    _signal(p, signal.SIGKILL if force else signal.SIGTERM)
                except ProcessLookupError:
                    pass
        left = _wait_gone(pairs, tm.proc_root)
        return {"name": g["name"], "verified": not left,
                "detail": f"{g['name']} has ended" if not left else
                          f"{g['name']}: {len(left)} of {len(pairs)} processes are still running. End with force?"}

    def set_priority(pid: int, start: int, nice: int) -> dict:
        p = _proc(tm, pid, start)
        if _own(p) and nice >= p.nice:
            os.setpriority(os.PRIO_PROCESS, pid, nice)
        else:
            r = subprocess.run(["pkexec", "renice", "-n", str(nice), "-p", str(pid)], capture_output=True, text=True,
                               timeout=120)
            if r.returncode != 0:
                raise ToolError("an administrator did not allow it" if r.returncode in (126, 127)
                                else (r.stderr or r.stdout).strip())
        now = os.getpriority(os.PRIO_PROCESS, pid)
        return {"pid": pid, "nice": now, "verified": now == nice, "detail": f"{p.name} priority is now {now}"}

    def service(name: str, action: str, scope: str = "system") -> dict:
        try:
            return SV.control(name, scope, action)
        except SV.ServiceError as exc:
            raise ToolError(str(exc)) from exc

    def startup_app(entry: str, enabled: bool) -> dict:
        try:
            e = ST.set_enabled(entry, enabled)
        except (ValueError, OSError) as exc:
            raise ToolError(str(exc)) from exc
        return {**e, "verified": e.get("enabled") == enabled,
                "detail": f"{e.get('name', entry)} is {'on' if e.get('enabled') else 'off'} at startup"}

    def open_location(pid: int, start: int) -> dict:
        p = _proc(tm, pid, start)
        if not p.exe:
            raise ToolError(f"the file of {p.name} cannot be read (another user's process)")
        subprocess.Popen(["dolphin", "--select", p.exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return {"path": p.exe}

    return {"end_task": end_task, "end_app": end_app, "set_priority": set_priority, "service": service,
            "startup_app": startup_app, "open_location": open_location}


def register(reg: Registry, tm: TaskManager) -> None:
    h = make_handlers(tm)

    def task_risk(a: dict) -> set[Risk]:
        p = tm.find(a["pid"])
        return {Risk.STOP} | ({Risk.ROOT} if p is not None and not _own(p) else set())

    def app_risk(a: dict) -> set[Risk]:
        g = next((g for g in tm.snapshot()["groups"] if g["key"] == a["key"]), None)
        others = g and any(p["uid"] != os.getuid() for p in g["procs"])
        return {Risk.STOP} | ({Risk.ROOT} if others else set())

    def prio_risk(a: dict) -> set[Risk]:
        p = tm.find(a["pid"])
        return {Risk.WRITE} if p is not None and _own(p) and a["nice"] >= p.nice else {Risk.ROOT}

    def svc_risk(a: dict) -> set[Risk]:
        r = {Risk.STOP} if a["action"] in ("stop", "restart") else {Risk.WRITE}
        return r | ({Risk.ROOT} if a.get("scope", "system") == "system" else set())

    def startup_risk(a: dict) -> set[Risk]:
        try:
            t = ST.target(a["entry"])
        except ValueError:
            return {Risk.OVERWRITE}
        if not t.is_file() or ST.parse(t).get(ST.OVERRIDE) == "true":
            return {Risk.WRITE}
        return {Risk.OVERWRITE}

    A = CALLER
    reg.add(Tool("end_task", A, "end one process", {"pid": I("process id", 1), "start": I("its start time", 0),
                 "force": {"type": "boolean", "description": "kill at once instead of asking it to quit"}},
                 ["pid", "start"], h["end_task"], task_risk, changes_state=True))
    reg.add(Tool("end_app", A, "end every process of an app", {"key": S("the app group's key"),
                 "force": {"type": "boolean", "description": "kill at once"}}, ["key"], h["end_app"], app_risk,
                 changes_state=True))
    reg.add(Tool("set_priority", A, "change a process's priority (nice value)",
                 {"pid": I("process id", 1), "start": I("its start time", 0), "nice": I("nice value", -20, 19)},
                 ["pid", "start", "nice"], h["set_priority"], prio_risk, changes_state=True))
    reg.add(Tool("service", A, "start, stop or restart a service",
                 {"name": S("unit name"), "action": S("what to do", enum=["start", "stop", "restart"]),
                  "scope": S("system or user service", enum=["system", "user"])}, ["name", "action"], h["service"],
                 svc_risk, changes_state=True))
    reg.add(Tool("startup_app", A, "turn an app on or off at login",
                 {"entry": S("autostart file name"), "enabled": {"type": "boolean", "description": "on or off"}},
                 ["entry", "enabled"], h["startup_app"], startup_risk, changes_state=True))
    reg.add(Tool("open_location", A, "show a process's program file in Dolphin",
                 {"pid": I("process id", 1), "start": I("its start time", 0)}, ["pid", "start"], h["open_location"]))


def describe(tm: TaskManager, tool: str, args: dict) -> str:
    """One plain sentence for the confirmation dialog."""
    if tool == "end_task":
        p = tm.find(args.get("pid", 0))
        n = f"{p.name} ({p.pid})" if p else f"process {args.get('pid')}"
        return f"{'Kill' if args.get('force') else 'End'} {n}? Anything it has not saved is lost."
    if tool == "end_app":
        g = next((g for g in tm.snapshot()["groups"] if g["key"] == args.get("key")), None)
        n = g["name"] if g else "this app"
        c = f" ({g['count']} processes)" if g and g["count"] > 1 else ""
        return f"{'Kill' if args.get('force') else 'End'} {n}{c}? Anything it has not saved is lost."
    if tool == "set_priority":
        return f"Set the priority of process {args.get('pid')} to {args.get('nice')}? Raising priority needs an administrator."
    if tool == "service":
        sc = args.get("scope", "system")
        return f"{args.get('action', '').capitalize()} the {sc} service {args.get('name')}?" + (
            " This needs an administrator." if sc == "system" else "")
    if tool == "startup_app":
        return f"Turn {args.get('entry')} {'on' if args.get('enabled') else 'off'} at login? This changes your own autostart file."
    return f"{tool} {args}"


__all__ = ["CONFIRM", "Confirmations", "TaskManager", "describe"]
