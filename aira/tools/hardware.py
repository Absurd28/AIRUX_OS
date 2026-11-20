"""AIRA and AIRUX Drivers: is the hardware working, why not, and fixing it.

Answers come only from airux-drivers' own diagnosis (sysfs, the kernel's firmware errors), never
from guesses. A fix installs drivers or firmware through the permission broker (confirmation), and
the helper asks polkit again.
"""
from __future__ import annotations

import json
import shutil
import subprocess

from .base import Risk, S, Tool, ToolError

CLI = "airux-drivers"
CATS = ["wifi", "ethernet", "bluetooth", "graphics", "sound", "camera", "input", "battery"]


def _cli(*args: str, timeout: float = 60) -> dict:
    if not shutil.which(CLI):
        raise ToolError("AIRUX Drivers is not installed")
    try:
        r = subprocess.run([CLI, *args, "--json"], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"AIRUX Drivers did not answer in {timeout:.0f} s") from exc
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ToolError(f"AIRUX Drivers failed: {r.stderr.strip()[:200] or 'no output'}") from exc


def _brief(f: dict) -> dict:
    out = {"part": f["title"], "device": f["device"] or None, "status": f["status"].replace("_", " "),
           "detail": f["detail"]}
    if f.get("fix"):
        out["fix_available"] = f["fix"]["label"]
    return out


def hardware(action: str, category: str = "") -> dict:
    if action in ("status", "check"):
        st = _cli("status")
        fs = st["findings"]
        if action == "check" and category:
            if category not in CATS:
                raise ToolError(f"say which part: {', '.join(CATS)}")
            fs = [f for f in fs if f["category"] == category]
        else:
            fs = [f for f in fs if f["status"] != "not_found"]
        return {"all_working": st["summary"]["all_working"],
                "parts": [_brief(f) for f in fs], "secure_boot": st["secure_boot"],
                "firmware_errors": [x["file"] for x in st["firmware_failures"]][:6]}
    if action == "fix":
        if category not in CATS:
            raise ToolError(f"say which part to fix: {', '.join(CATS)}")
        out = _cli("fix", category, timeout=1800)
        if not out.get("ok"):
            raise ToolError(f"the fix did not work: {out.get('error')}")
        res = {"fixed": category, "did": out["fix"]["label"], "restart_needed": bool(out.get("restart_needed"))}
        if out.get("enroll_key"):
            res["secure_boot"] = ("Secure Boot is on: open AIRUX Drivers and choose 'Enroll the driver key', "
                                  "then restart and confirm on the blue MOK screen")
        return res
    raise ToolError(f"unknown hardware action {action}")


def _risk(a: dict) -> set[Risk]:
    if a.get("action") == "fix":
        return {Risk.INSTALL, Risk.ROOT}         # installs packages / switches radios, as root
    return {Risk.READ}


def _readback(a: dict, result) -> tuple[bool, str]:
    if a.get("action") != "fix":
        return True, "read"
    st = _cli("status")
    f = next((x for x in st["findings"] if x["category"] == a["category"]), None)
    if f is None:
        return False, "no such part any more"
    if result.get("restart_needed"):
        # drivers and firmware start at the next boot: the packages being there is what can be checked now
        pkgs = [w for w in result["did"].split() if w.startswith(("firmware-", "nvidia-", "intel-", "amd64-"))]
        r = subprocess.run(["dpkg-query", "-W", "-f=${Package} ${Status}\\n", *pkgs], capture_output=True, text=True)
        ok = all(f"{p} install ok installed" in r.stdout for p in pkgs) if pkgs else True
        return ok, f"installed: {', '.join(pkgs) or 'done'}; restart to use it" if ok else "the packages are not installed"
    return f["status"] in ("working",), f"{f['title']} is now {f['status'].replace('_', ' ')}"


def register(reg) -> None:
    reg.add(Tool("hardware", "system",
                 "AIRUX Drivers: whether this computer's hardware works (Wi-Fi, Ethernet, Bluetooth, graphics, "
                 "sound, camera, touchpad, battery), why a part does not, and fixing it (firmware, NVIDIA driver, "
                 "switching Wi-Fi/Bluetooth on)",
                 {"action": S("status: every part at once (one call is enough); check: one part; fix: one part",
                              enum=["status", "check", "fix"]),
                  "category": S("which part, for check and fix (not needed for status)", enum=CATS)},
                 ["action"], hardware, _risk, readback=_readback,
                 changes_state=lambda a: a.get("action") == "fix"))
