"""Services: systemd system and user units (list, start, stop, restart)."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

UNIT_RE = re.compile(r"^[A-Za-z0-9:_.@\\-]{1,200}\.service$")
POLKIT_RULE = Path("/usr/share/polkit-1/rules.d/50-aira.rules")


class ServiceError(Exception):
    pass


def _systemctl(scope: str) -> list[str]:
    return ["systemctl", "--user"] if scope == "user" else ["systemctl"]


def listing(scope: str = "system") -> list[dict]:
    cmd = _systemctl(scope) + ["list-units", "--type=service", "--all", "--no-pager", "--output=json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        rows = json.loads(r.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    return [{"unit": u.get("unit", ""), "description": u.get("description", ""), "load": u.get("load", ""),
             "active": u.get("active", ""), "sub": u.get("sub", ""), "scope": scope}
            for u in rows if u.get("load") != "not-found"]


def check_unit(name: str) -> str:
    unit = name if name.endswith(".service") else name + ".service"
    if not UNIT_RE.match(unit):
        raise ServiceError(f"invalid service name: {name!r}")
    return unit


def status(name: str, scope: str = "system") -> dict:
    unit = check_unit(name)
    r = subprocess.run(_systemctl(scope) + ["show", unit, "--no-pager",
                       "--property=LoadState,ActiveState,SubState,Description,MainPID"],
                       capture_output=True, text=True, timeout=20)
    props = dict(line.split("=", 1) for line in r.stdout.strip().splitlines() if "=" in line)
    if props.get("LoadState") in (None, "not-found"):
        raise ServiceError(f"no such {scope} service: {unit}")
    return {"unit": unit, "scope": scope, **props}


def control(name: str, scope: str, action: str) -> dict:
    if action not in ("start", "stop", "restart"):
        raise ServiceError(f"unknown action {action!r}")
    unit = check_unit(name)
    status(unit, scope)
    cmd = _systemctl(scope) + ["--no-ask-password", action, unit]
    if scope == "system" and os.geteuid() != 0 and not POLKIT_RULE.exists():
        cmd = ["sudo", "-n", *cmd]          # development machines without AIRUX's polkit rule
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise ServiceError(f"systemctl {action} {unit} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    st = status(unit, scope)
    want = "inactive" if action == "stop" else "active"
    st["verified"] = st.get("ActiveState") == want
    st["detail"] = f"{unit} is {st.get('ActiveState')} (expected {want})"
    return st
