"""AIRA's `phone` tool (Assistant agent): the user's paired phone through AIROX Connect.

  send_file   send a file from the PC to the phone (data leaves the PC: confirmed first)
  ring        make the phone ring, to find it
  status      paired phones, whether they are connected, their battery

It talks to airox-connectd's local API (127.0.0.1, token in ~/.local/state/airox-connect/ui.token).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .base import Registry, Risk, S, Tool, ToolError, exists_path, expand


def token_file() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "airox-connect" / "ui.token"


def call(method: str, params: dict | None = None, timeout: float = 30) -> dict:
    try:
        port, token = token_file().read_text().split()
    except (OSError, ValueError):
        raise ToolError("AIROX Connect is not running on this PC") from None
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api", data=json.dumps({"method": method, "params": params or {}}).encode(),
                                 headers={"Content-Type": "application/json", "X-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ToolError(f"AIROX Connect did not answer: {exc}") from None


def phone(action: str, path: str = "") -> dict:
    if action == "status":
        phones = call("phone_status")["phones"]
        if not phones:
            raise ToolError("no phone is paired with AIROX Connect")
        return {"phones": [{"name": p["name"], "connected": p["online"],
                            "battery_percent": (p.get("status") or {}).get("battery"),
                            "charging": (p.get("status") or {}).get("charging")} for p in phones]}
    if action == "ring":
        out = call("ring")
        if not out.get("ok"):
            raise ToolError(out.get("error") or "the phone could not be reached")
        return {"rung": out["rung"]}
    if action == "send_file":
        if not path:
            raise ToolError("say which file to send")
        p = expand(path)
        if not p.is_file():
            raise ToolError(f"no such file: {path}")
        # AIRA's broker has already asked the user (send_off_machine); connectd logs it too
        out = call("send", {"paths": [str(p)], "confirmed_by_aira": True}, timeout=60)
        if not out.get("ok"):
            raise ToolError(out.get("error") or "; ".join(r.get("error") or "" for r in out.get("results", [])))
        return {"sent": p.name, "to": len(out["results"])}
    raise ToolError(f"unknown phone action {action}")


def register(reg: Registry) -> None:
    reg.add(Tool("phone", "assistant", "the user's paired phone (AIROX Connect): send a file to it, ring it to find it, "
                 "or its status and battery",
                 {"action": S("what to do", enum=["send_file", "ring", "status"]), "path": S("file to send, for send_file")},
                 ["action"], phone,
                 lambda a: {Risk.SEND_OFF} if a.get("action") == "send_file" else (
                     {Risk.WRITE} if a.get("action") == "ring" else {Risk.READ}),
                 entities=lambda a: [exists_path(a["path"], "file")] if a.get("action") == "send_file" and a.get("path")
                 else ([("file", "(none given)", False)] if a.get("action") == "send_file" else [])))
