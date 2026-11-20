"""AIRA's `security` tool against a real Phagos core over a real socket (no root, no fanotify)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from aira.tools import security as sec
from aira.tools.base import Risk, ToolError
from phagos import daemon as D
from test_guard import call, make, reply, route

RULES = Path(__file__).resolve().parents[2] / "phagos" / "rules"
WEBSHELL = "<?php eval(base64_decode($_POST['x'])); ?>"


@pytest.fixture
def phagos(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    paths = D.Paths(state=tmp_path / "state", rules=(RULES,), socket=tmp_path / "run" / "phagos.sock",
                    dpkg_info=str(tmp_path), homes=(str(home),))
    ph = D.Phagos(paths, seed=1, train=False)
    ph.authorized = lambda peer: False                  # this test user is not an administrator
    srv = D.serve_socket(ph, paths.socket)
    monkeypatch.setattr(sec, "SOCKET", str(paths.socket))
    monkeypatch.setenv("HOME", str(home))
    yield ph, home
    srv.close()


def test_status_scan_and_history(phagos):
    ph, home = phagos
    st = sec.security("status")
    assert st["real_time_protection"] == "on" and st["signature_rules"] >= 10 and st["files_in_quarantine"] == 0
    (home / "Downloads" / "shell.php").write_text(WEBSHELL)
    out = sec.security("scan", path="~/Downloads")
    assert out["files_checked"] == 1
    assert out["threats_found"][0]["threat"] == "Webshell.PHP" and "quarantined" in out["threats_found"][0]["action_taken"]
    assert not (home / "Downloads" / "shell.php").exists()
    ok, why = sec._readback({"action": "scan", "path": "~/Downloads"}, out)
    assert ok and "1 quarantined" in why
    kinds = [e["kind"] for e in sec.security("history")["events"]]
    assert "threat" in kinds and "scan" in kinds


def test_quarantine_is_read_back_and_restore_needs_an_admin(phagos):
    ph, home = phagos
    f = home / "Downloads" / "odd.sh"
    f.write_text("#!/bin/sh\necho hi\n")
    out = sec.security("quarantine", path=str(f))
    ok, why = sec._readback({"action": "quarantine", "path": str(f)}, out)
    assert ok and not f.exists()
    with pytest.raises(ToolError, match="administrator"):
        sec.security("restore", id=out["quarantine_id"])


def test_risks_by_action():
    assert sec._risk({"action": "status"}) == {Risk.READ}
    assert sec._risk({"action": "scan", "path": "~"}) == {Risk.READ}
    assert sec._risk({"action": "quarantine", "path": "~/x"}) == {Risk.DELETE}
    assert sec._risk({"action": "restore", "id": "abc"}) == {Risk.OVERWRITE, Risk.ROOT}


def test_no_phagos_is_reported_plainly(monkeypatch, tmp_path):
    monkeypatch.setattr(sec, "SOCKET", str(tmp_path / "none.sock"))
    with pytest.raises(ToolError, match="not running"):
        sec.security("status")


def test_aira_asks_before_quarantining(phagos, tmp_path):
    ph, home = phagos
    f = home / "Downloads" / "tool.sh"
    f.write_text("#!/bin/sh\necho hi\n")
    asked = []
    q = call("security", action="quarantine", path=str(f))
    orch, llm, executed = make(home, tmp_path, [route("system", "quarantine the file"), q, q, q,
                                                reply(f"{f} is in quarantine.")],
                               confirm=lambda agent, tool, args, risks: (asked.append((tool, args["action"])), True)[1])
    turn = orch.handle(f"Quarantine {f}")
    assert asked == [("security", "quarantine")] and executed == ["security"]
    assert not f.exists()
    run = turn.runs[0].tool_calls[0]
    assert run.verified                                  # read back: gone and listed in quarantine
