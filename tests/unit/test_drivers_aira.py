"""AIRA's `hardware` tool against a stand-in `airux-drivers` command (answers like the Acer Nitro)."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from aira.tools import hardware as hw
from aira.tools.base import Risk, ToolError
from test_guard import call, make, reply, route

FINDINGS = [
    {"category": "wifi", "title": "Wi-Fi", "device": "Intel Raptor Lake PCH CNVi WiFi", "status": "needs_firmware",
     "detail": "iwlwifi could not load iwlwifi-so-a0-gf-a0-89.ucode.",
     "fix": {"action": "install", "args": ["firmware-iwlwifi"], "label": "Install firmware-iwlwifi", "needs_restart": True}},
    {"category": "ethernet", "title": "Ethernet", "device": "Realtek RTL8111", "status": "working",
     "detail": "enp44s0 (r8169), cable connected.", "fix": None},
    {"category": "camera", "title": "Camera", "device": "", "status": "not_found", "detail": "No camera found.", "fix": None},
]


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"installed": False}))
    script = tmp_path / "bin" / "airux-drivers"
    script.parent.mkdir()
    script.write_text(f"""#!{sys.executable}
import json, sys
state = json.load(open({str(state)!r}))
findings = {json.dumps(FINDINGS)!r}
findings = json.loads(findings)
if state["installed"]:
    findings[0].update(status="working", detail="wlan0 (iwlwifi), connected.", fix=None)
args = sys.argv[1:]
if args[0] == "status":
    bad = [f for f in findings if f["status"] not in ("working", "not_found")]
    print(json.dumps({{"summary": {{"all_working": not bad, "problems": len(bad), "fixable": len(bad)}},
                      "findings": findings, "secure_boot": "enabled",
                      "firmware_failures": [] if state["installed"] else [{{"file": "iwlwifi-so-a0-gf-a0-89.ucode"}}]}}))
elif args[0] == "fix":
    state["installed"] = True
    json.dump(state, open({str(state)!r}, "w"))
    print(json.dumps({{"ok": True, "installed": ["firmware-iwlwifi"], "restart_needed": False,
                      "fix": {{"label": "Install firmware-iwlwifi"}}}}))
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")
    return state


def test_status_and_check_come_from_the_diagnosis(fake_cli):
    st = hw.hardware("status")
    assert st["all_working"] is False and st["secure_boot"] == "enabled"
    assert [p["part"] for p in st["parts"]] == ["Wi-Fi", "Ethernet"]            # absent parts are left out
    assert st["parts"][0]["fix_available"] == "Install firmware-iwlwifi"
    wifi = hw.hardware("check", "wifi")
    assert wifi["parts"][0]["status"] == "needs firmware" and "iwlwifi" in wifi["parts"][0]["detail"]
    with pytest.raises(ToolError, match="which part"):
        hw.hardware("check", "toaster")


def test_fix_is_risky_and_read_back(fake_cli):
    assert hw._risk({"action": "status"}) == {Risk.READ}
    assert hw._risk({"action": "fix", "category": "wifi"}) == {Risk.INSTALL, Risk.ROOT}
    out = hw.hardware("fix", "wifi")
    ok, why = hw._readback({"action": "fix", "category": "wifi"}, out)
    assert ok and "working" in why


def test_no_airux_drivers_is_reported_plainly(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ToolError, match="not installed"):
        hw.hardware("status")


def test_aira_asks_before_fixing(fake_cli, tmp_path):
    asked = []
    home = tmp_path / "home"
    home.mkdir()
    f = call("hardware", action="fix", category="wifi")
    orch, llm, executed = make(home, tmp_path, [route("system", "fix the wifi"), f, f, f,
                                                reply("Wi-Fi is fixed: firmware-iwlwifi was installed.")],
                               confirm=lambda agent, tool, args, risks: (asked.append(tool), True)[1])
    turn = orch.handle("My Wi-Fi doesn't work, fix it.")
    assert asked == ["hardware"] and executed == ["hardware"]
    assert turn.runs[0].tool_calls[0].verified
