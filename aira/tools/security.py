"""AIRA and Phagos (AIRUX's antivirus): status, scans, history, quarantine, through phagosd's socket.

AIRA answers only from what Phagos returns (grounding); quarantine and restore go through the
permission broker like any risky action, and Phagos checks its own permissions again (polkit).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import time

from .base import Risk, S, Tool, ToolError, exists_path, expand

SOCKET = os.environ.get("PHAGOS_SOCKET", "/run/phagos/phagos.sock")


def call(method: str, timeout: float = 30, **params) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCKET)
        s.sendall((json.dumps({"method": method, "params": params}) + "\n").encode())
        line = s.makefile("rb").readline()
    except OSError as exc:
        raise ToolError("Phagos (the antivirus) is not running") from exc
    finally:
        s.close()
    out = json.loads(line or b"{}")
    if not out.pop("ok", False):
        raise ToolError(f"Phagos: {out.get('error', 'no answer')}")
    return out


def _when(ts) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


def security(action: str, path: str = "", id: str = "") -> dict:
    if action == "status":
        s = call("status")
        e = s["engines"]
        return {"real_time_protection": "on" if s["realtime"] else "off",
                "protected": s["protected"],
                "programs_paused_waiting_for_you": [{"program": p.get("script") or p["exe"], "pid": p["pid"],
                                                     "why": p["reasons"]} for p in s["paused"]],
                "threats_last_30_days": s["threats_30_days"], "files_in_quarantine": s["quarantined"],
                "signature_rules": e["rules"], "immune_model": e["self_model"], "immune_detectors": e["detectors"],
                "ransomware_decoys": s["ransomware_shield"]["decoys"],
                "last_quick_scan": _when((s["last_scans"].get("quick") or {}).get("ts")),
                "last_full_scan": _when((s["last_scans"].get("full") or {}).get("ts"))}
    if action == "scan":
        params = {"kind": "custom", "path": str(expand(path))} if path else {"kind": "quick"}
        call("scan", **params)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            st = call("scan_status")["scan"]
            if st["done"]:
                return {"scan": st["kind"], "files_checked": st["scanned"],
                        "threats_found": [{"file": f["path"], "threat": f["threat"], "level": f["level"],
                                           "action_taken": f["action"]} for f in st["found"]]}
            time.sleep(1)
        raise ToolError("the scan is still running after 15 minutes; Phagos keeps going in the background")
    if action == "history":
        ev = call("history", limit=15)["events"]
        return {"events": [{"when": _when(e["ts"]), "kind": e["kind"], "threat": e.get("threat"), "file": e.get("path"),
                            "action": e.get("action")} for e in ev]}
    if action == "quarantine":
        if not path:
            raise ToolError("say which file to quarantine")
        q = call("quarantine_file", timeout=300, path=str(expand(path)))["quarantined"]
        return {"quarantined": q["path"], "quarantine_id": q["id"], "threat": q["threat"]}
    if action == "restore":
        if not id:
            raise ToolError("say which quarantine item to restore (its id)")
        r = call("quarantine_restore", timeout=300, id=id)["restored"]
        return {"restored": r["path"], "threat": r["threat"]}
    raise ToolError(f"unknown security action {action}")


def _risk(a: dict) -> set[Risk]:
    act = a.get("action")
    if act == "quarantine":
        return {Risk.DELETE}                      # the file leaves its folder (it can be restored)
    if act == "restore":
        return {Risk.OVERWRITE, Risk.ROOT}        # puts a flagged file back; Phagos asks for an admin too
    return {Risk.READ}


def _entities(a: dict) -> list:
    if a.get("action") in ("scan", "quarantine") and a.get("path"):
        return [exists_path(a["path"], "file" if a["action"] == "quarantine" else "path")]
    return []


def _readback(a: dict, result) -> tuple[bool, str]:
    if a.get("action") == "scan":
        # Phagos quarantines what it finds (its own policy): every file it says it moved must be gone
        moved = [t["file"] for t in result.get("threats_found", []) if "quarantined" in (t.get("action_taken") or "")]
        still = [p for p in moved if os.path.exists(p)]
        return not still, (f"{len(moved)} quarantined file(s) gone from their folders" if not still
                           else f"still present: {', '.join(still)}")
    if a.get("action") == "quarantine":
        gone = not expand(a["path"]).exists()
        listed = any(i["id"] == result["quarantine_id"] for i in call("quarantine_list")["items"])
        return gone and listed, f"{a['path']} {'is' if gone and listed else 'is not'} in quarantine"
    if a.get("action") == "restore":
        back = os.path.exists(result["restored"])
        return back, f"{result['restored']} {'is back' if back else 'is not back'}"
    return True, "read"


def register(reg) -> None:
    reg.add(Tool("security", "system",
                 "Phagos, AIRUX's antivirus: status (is the computer protected), scan (quick, or a folder), "
                 "history (threats found), quarantine a file, restore a quarantined item by id",
                 {"action": S("what to do", enum=["status", "scan", "history", "quarantine", "restore"]),
                  "path": S("folder to scan, or file to quarantine"), "id": S("quarantine id, for restore")},
                 ["action"], security, _risk, entities=_entities, readback=_readback,
                 # a scan changes things too: Phagos quarantines what it finds, so it is read back
                 changes_state=lambda a: a.get("action") in ("scan", "quarantine", "restore")))
