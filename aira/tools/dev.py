"""Dev agent tools: git, tests, commands, containers (Podman), Python environments."""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .base import (I, Registry, Risk, S, Tool, ToolError, exists_dir, expand, parent_dir, run,
                   truncate)

NAME_RE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


def _repo(path: str) -> Path:
    p = expand(path)
    if not p.exists():
        raise ToolError(f"no such folder: {p}")
    try:
        top = run(["git", "-C", str(p), "rev-parse", "--show-toplevel"]).strip()
    except ToolError:
        raise ToolError(f"not a git repository: {p}") from None
    return Path(top)


def git_status(repo: str = ".") -> dict:
    r = _repo(repo)
    branch = run(["git", "-C", str(r), "branch", "--show-current"]).strip()
    porcelain = run(["git", "-C", str(r), "status", "--porcelain=v1"]).splitlines()
    return {"repo": str(r), "branch": branch, "clean": not porcelain,
            "changes": [{"status": l[:2].strip(), "path": l[3:]} for l in porcelain[:100]]}


def git_log(repo: str = ".", count: int = 10) -> dict:
    r = _repo(repo)
    out = run(["git", "-C", str(r), "log", f"-{count}", "--format=%h%x1f%an%x1f%ad%x1f%s", "--date=short"])
    commits = [dict(zip(("hash", "author", "date", "subject"), l.split("\x1f"))) for l in out.splitlines()]
    return {"repo": str(r), "commits": commits}


def git_commit(repo: str, message: str, all_changes: bool = True) -> dict:
    r = _repo(repo)
    if all_changes:
        run(["git", "-C", str(r), "add", "-A"])
    if not run(["git", "-C", str(r), "diff", "--cached", "--name-only"]).strip():
        raise ToolError("nothing to commit")
    ident = subprocess.run(["git", "-C", str(r), "var", "GIT_COMMITTER_IDENT"], capture_output=True, text=True)
    if ident.returncode != 0:
        raise ToolError("git does not know your name and email yet, so it cannot commit. Tell me them, or run "
                        "git config --global user.name and git config --global user.email")
    run(["git", "-C", str(r), "commit", "-m", message])
    head = run(["git", "-C", str(r), "log", "-1", "--format=%h %s"]).strip()
    return {"repo": str(r), "committed": head}


def _detect_test_command(p: Path) -> list[str]:
    if (p / "pytest.ini").exists() or (p / "pyproject.toml").exists() or (p / "tests").is_dir() \
            or any(p.glob("test_*.py")):
        return [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
    if (p / "package.json").exists():
        return ["npm", "test", "--silent"]
    if (p / "Cargo.toml").exists():
        return ["cargo", "test"]
    if (p / "Makefile").exists():
        return ["make", "test"]
    raise ToolError(f"no test setup found in {p} (looked for pytest, npm, cargo, make)")


def run_tests(path: str = ".") -> dict:
    p = expand(path)
    if not p.is_dir():
        raise ToolError(f"no such folder: {p}")
    cmd = _detect_test_command(p)
    try:
        r = subprocess.run(cmd, cwd=p, capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        raise ToolError(f"test runner not installed: {cmd[0]}") from None
    except subprocess.TimeoutExpired:
        raise ToolError("tests timed out after 15 minutes") from None
    out = (r.stdout + r.stderr).strip()
    return {"path": str(p), "command": " ".join(cmd[:4]), "passed": r.returncode == 0,
            "exit_code": r.returncode, "output_tail": truncate(out[-6000:], 6000)}


def run_command(command: str, cwd: str = "~") -> dict:
    d = expand(cwd)
    if not d.is_dir():
        raise ToolError(f"no such folder: {d}")
    r = subprocess.run(["bash", "-lc", command], cwd=d, capture_output=True, text=True, timeout=600)
    return {"command": command, "cwd": str(d), "exit_code": r.returncode,
            "stdout": truncate(r.stdout, 4000), "stderr": truncate(r.stderr, 2000)}


def _command_risk(args: dict) -> set[Risk]:
    risks = {Risk.EXEC}
    words = shlex.split(args.get("command", ""), posix=True) if args.get("command") else []
    if any(w in ("sudo", "su", "pkexec") for w in words):
        risks.add(Risk.ROOT)
    return risks


def _podman() -> str:
    exe = shutil.which("podman")
    if not exe:
        raise ToolError("podman is not installed")
    return exe


def container_list(all_containers: bool = True) -> dict:
    out = run([_podman(), "ps", "--format", "json"] + (["--all"] if all_containers else []))
    data = json.loads(out or "[]")
    return {"containers": [{"name": (c.get("Names") or ["?"])[0], "image": c.get("Image"),
                            "state": c.get("State"), "status": c.get("Status")} for c in data]}


def containers(action: str = "list", name: str = "") -> dict:
    if action == "list":
        return container_list()
    if not name:
        raise ToolError(f"say which container to {action}")
    return container_control(name, action)


def container_control(name: str, action: str) -> dict:
    if not set(name) <= NAME_RE_CHARS:
        raise ToolError(f"invalid container name: {name!r}")
    names = [c["name"] for c in container_list()["containers"]]
    if name not in names:
        raise ToolError(f"no container called {name!r}; existing: {names}")
    run([_podman(), action, name], timeout=120)
    state = [c for c in container_list()["containers"] if c["name"] == name]
    return {"container": name, "action": action, "state": state[0]["state"] if state else "removed"}


def python_env(action: str, path: str, package: str = "") -> dict:
    p = expand(path)
    py = p / "bin" / "python"
    if action == "create":
        if p.exists():
            raise ToolError(f"already exists: {p}")
        run([sys.executable, "-m", "venv", str(p)], timeout=180)
        return {"created": str(p), "python": run([str(py), "--version"]).strip()}
    if not py.exists():
        raise ToolError(f"not a Python virtual environment: {p}")
    if action == "list":
        pkgs = json.loads(run([str(py), "-m", "pip", "list", "--format=json"], timeout=60))
        return {"env": str(p), "packages": [f"{x['name']}=={x['version']}" for x in pkgs]}
    if action == "install":
        if not package or not all(ch.isalnum() or ch in "-_.=<>[],~" for ch in package):
            raise ToolError(f"invalid package spec: {package!r}")
        run([str(py), "-m", "pip", "install", package], timeout=900)
        return {"env": str(p), "installed": package}
    raise ToolError(f"unknown action {action}")


def _is_repo(path: str) -> bool:
    try:
        _repo(path)
        return True
    except ToolError:
        return False


def _repo_entities(a: dict) -> list:
    folder = exists_dir(a["repo"])
    if not folder[2]:
        return [folder]
    return [("git repository", a["repo"], _is_repo(a["repo"]))]


def _commit_readback(a: dict, result) -> tuple[bool, str]:
    head = run(["git", "-C", result["repo"], "log", "-1", "--format=%h %s"]).strip()
    ok = head == result["committed"]
    return ok, f"HEAD is {head}" if ok else f"HEAD is {head}, expected {result['committed']}"


def _container_exists(name: str) -> bool:
    try:
        return name in [c["name"] for c in container_list()["containers"]]
    except ToolError:
        return False


def _container_readback(a: dict, result) -> tuple[bool, str]:
    names = {c["name"]: c["state"] for c in container_list()["containers"]}
    if a["action"] == "rm":
        return a["name"] not in names, f"{a['name']} {'removed' if a['name'] not in names else 'still exists'}"
    want = "exited" if a["action"] == "stop" else "running"
    st = names.get(a["name"], "missing")
    return st == want, f"{a['name']} is {st} (expected {want})"


def _env_entities(a: dict) -> list:
    if a["action"] == "create":
        return [parent_dir(a["path"])]
    try:
        ok = (expand(a["path"]) / "bin" / "python").exists()
    except ToolError:
        ok = False
    return [("Python environment", a["path"], ok)]


def _env_readback(a: dict, result) -> tuple[bool, str]:
    py = expand(a["path"]) / "bin" / "python"
    if a["action"] == "create":
        return py.exists(), f"{py} {'exists' if py.exists() else 'missing'}"
    if a["action"] == "install":
        name = re.split(r"[=<>\[~ ]", a.get("package", ""), maxsplit=1)[0]
        r = subprocess.run([str(py), "-m", "pip", "show", name], capture_output=True, text=True)
        return r.returncode == 0, f"{name} {'is' if r.returncode == 0 else 'is not'} installed"
    return True, "read only"


UNIT_RE = re.compile(r"^[A-Za-z0-9@._:\-]+$")


def _proc_exists(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _unit_exists(unit: str) -> bool:
    if not UNIT_RE.match(unit):
        return False
    r = subprocess.run(["systemctl", "--user", "show", unit, "-p", "LoadState", "--value"],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip() not in ("", "not-found")


def make_watch_job(vault):
    def watch_job(label: str, pid: int = 0, unit: str = "") -> dict:
        if not pid and not unit:
            raise ToolError("give the process id or the systemd unit of the job to watch")
        now = vault.clock().isoformat(timespec="seconds")
        with vault._lock:
            cur = vault.db.execute("INSERT INTO jobs(label, pid, unit, started) VALUES (?,?,?,?)",
                                   (label, pid or None, unit or None, now))
            vault.db.commit()
        return {"id": cur.lastrowid, "label": label, "pid": pid or None, "unit": unit or None}

    def entities(a: dict) -> list:
        out = []
        if a.get("pid"):
            out.append(("process", str(a["pid"]), _proc_exists(int(a["pid"]))))
        if a.get("unit"):
            out.append(("systemd unit", a["unit"], _unit_exists(a["unit"])))
        return out

    def readback(a: dict, result) -> tuple[bool, str]:
        r = vault.db.execute("SELECT id FROM jobs WHERE id=? AND finished IS NULL", (result["id"],)).fetchone()
        return bool(r), f"job {result['id']} is being watched" if r else "watch not stored"

    return watch_job, entities, readback


def register(reg: Registry, vault=None) -> None:
    A = "dev"
    reg.add(Tool("git_status", A, "branch and uncommitted changes of a git repository",
                 {"repo": S("repository folder")}, ["repo"], git_status, entities=_repo_entities))
    reg.add(Tool("git_log", A, "recent commits of a git repository",
                 {"repo": S("repository folder"), "count": I("how many", 1, 100)}, ["repo"], git_log,
                 entities=_repo_entities))
    reg.add(Tool("git_commit", A, "commit changes in a git repository",
                 {"repo": S("repository folder"), "message": S("commit message"),
                  "all_changes": {"type": "boolean"}}, ["repo", "message"], git_commit,
                 frozenset({Risk.WRITE}), entities=_repo_entities, readback=_commit_readback, changes_state=True))
    reg.add(Tool("run_tests", A, "run a project's test suite (pytest, npm, cargo or make) and report failures",
                 {"path": S("project folder")}, ["path"], run_tests, frozenset({Risk.WRITE}),
                 entities=lambda a: [exists_dir(a["path"], "project folder")]))
    reg.add(Tool("run_command", A, "run a shell command in a terminal (always asks first)",
                 {"command": S("shell command"), "cwd": S("working folder")}, ["command"], run_command,
                 _command_risk, entities=lambda a: [exists_dir(a["cwd"])] if a.get("cwd") else [],
                 changes_state=True))  # effect cannot be read back in general: never reported "done"
    reg.add(Tool("containers", A, "list Podman containers, or start, stop, restart or remove one",
                 {"action": S("what to do", enum=["list", "start", "stop", "restart", "rm"]),
                  "name": S("container name (not needed for list)")}, ["action"], containers,
                 lambda a: {Risk.DELETE} if a.get("action") in ("stop", "rm")
                 else ({Risk.READ} if a.get("action") == "list" else {Risk.WRITE}),
                 entities=lambda a: [] if a.get("action") == "list"
                 else [("container", a.get("name", ""), _container_exists(a.get("name", "")))],
                 readback=lambda a, r: (True, "read only") if a.get("action") == "list" else _container_readback(a, r),
                 changes_state=lambda a: a.get("action") != "list"))
    if vault is not None:
        fn, ent, rb = make_watch_job(vault)
        reg.add(Tool("watch_job", A, "watch a running job (process id or systemd unit) and tell the user when "
                     "it finishes or fails", {"label": S("short name for the job"), "pid": I("process id", 1),
                                              "unit": S("systemd user unit")}, ["label"], fn,
                     frozenset({Risk.WRITE}), entities=ent, readback=rb, changes_state=True))
    reg.add(Tool("python_env", A, "create a Python virtual environment, list or install its packages",
                 {"action": S("what to do", enum=["create", "list", "install"]), "path": S("environment folder"),
                  "package": S("package to install, e.g. numpy==2.1.0")}, ["action", "path"], python_env,
                 lambda a: {Risk.INSTALL, Risk.SEND_OFF} if a.get("action") == "install"
                 else ({Risk.WRITE} if a.get("action") == "create" else {Risk.READ}),
                 entities=_env_entities, readback=_env_readback,
                 changes_state=lambda a: a.get("action") in ("create", "install")))
