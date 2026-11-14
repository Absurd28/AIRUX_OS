"""Dev agent tools: git, tests, commands, containers (Podman), Python environments."""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .base import I, Registry, Risk, S, Tool, ToolError, expand, run, truncate

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


def register(reg: Registry) -> None:
    A = "dev"
    reg.add(Tool("git_status", A, "branch and uncommitted changes of a git repository",
                 {"repo": S("repository folder")}, ["repo"], git_status))
    reg.add(Tool("git_log", A, "recent commits of a git repository",
                 {"repo": S("repository folder"), "count": I("how many", 1, 100)}, ["repo"], git_log))
    reg.add(Tool("git_commit", A, "commit changes in a git repository",
                 {"repo": S("repository folder"), "message": S("commit message"),
                  "all_changes": {"type": "boolean"}}, ["repo", "message"], git_commit,
                 frozenset({Risk.WRITE})))
    reg.add(Tool("run_tests", A, "run a project's test suite (pytest, npm, cargo or make) and report failures",
                 {"path": S("project folder")}, ["path"], run_tests, frozenset({Risk.WRITE})))
    reg.add(Tool("run_command", A, "run a shell command in a terminal (always asks first)",
                 {"command": S("shell command"), "cwd": S("working folder")}, ["command"], run_command,
                 _command_risk))
    reg.add(Tool("container_list", A, "list Podman containers and their state",
                 {"all_containers": {"type": "boolean"}}, [], container_list))
    reg.add(Tool("container_control", A, "start, stop, restart or remove a Podman container",
                 {"name": S("container name"), "action": S("what to do", enum=["start", "stop", "restart", "rm"])},
                 ["name", "action"], container_control,
                 lambda a: {Risk.DELETE} if a.get("action") in ("stop", "rm") else {Risk.WRITE}))
    reg.add(Tool("python_env", A, "create a Python virtual environment, list or install its packages",
                 {"action": S("what to do", enum=["create", "list", "install"]), "path": S("environment folder"),
                  "package": S("package to install, e.g. numpy==2.1.0")}, ["action", "path"], python_env,
                 lambda a: {Risk.INSTALL, Risk.SEND_OFF} if a.get("action") == "install"
                 else ({Risk.WRITE} if a.get("action") == "create" else {Risk.READ})))
