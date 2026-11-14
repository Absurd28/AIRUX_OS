"""Unit tests for the permission broker, tool registry and tools (no model needed)."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from aira.broker import Broker, deny_all
from aira.config import Config
from aira.tools import build_registry
from aira.memory.embed import HashEmbedder
from aira.memory.vault import Vault
from aira.tools.assistant import parse_ics
from aira.tools.base import AGENTS, CONFIRM, MAX_TOOLS_PER_AGENT, Registry, Risk, Tool, tool_cap
from aira.tools.timeparse import parse_when

NOW = dt.datetime(2026, 11, 14, 10, 30)  # a Saturday


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "notes").mkdir(parents=True)
    (h / "notes" / "lab.md").write_text("Lab notes\nThe transformer run diverged at step 400.\n")
    (h / "old.log").write_text("old\n")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return h


@pytest.fixture
def setup(home, tmp_path):
    cfg = Config(server_url="http://127.0.0.1:9", data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    vault = Vault(cfg.data_dir / "vault.db", "test-key", embedder=HashEmbedder(), clock=lambda: NOW)
    reg = build_registry(cfg, vault)
    calls: list[str] = []
    for t in reg.tools.values():  # spy on every handler
        orig = t.handler

        def spy(*a, _orig=orig, _name=t.name, **k):
            calls.append(_name)
            return _orig(*a, **k)
        t.handler = spy
    return cfg, reg, calls


def log_entries(cfg: Config) -> list[dict]:
    return [json.loads(l) for l in cfg.action_log.read_text().splitlines()] if cfg.action_log.exists() else []


# ---------- registry ----------

def test_registry_shape(setup):
    _, reg, _ = setup
    assert 30 <= len(reg.tools) <= 40
    for a in AGENTS:
        assert 1 <= len(reg.for_agent(a)) <= tool_cap(a)
    assert tool_cap("desktop") == 9 and all(tool_cap(a) == MAX_TOOLS_PER_AGENT for a in AGENTS if a != "desktop")
    for t in reg.tools.values():
        s = t.args_schema()
        assert set(t.required) <= set(t.params), t.name
        assert s["additionalProperties"] is False


def test_registry_enforces_tool_cap():
    reg = Registry()
    for i in range(MAX_TOOLS_PER_AGENT):
        reg.add(Tool(f"t{i}", "dev", "x", {}, [], lambda: None))
    with pytest.raises(ValueError):
        reg.add(Tool("t9", "dev", "x", {}, [], lambda: None))


# ---------- broker ----------

def test_broker_rejects_unknown_tool_and_bad_args(setup):
    cfg, reg, calls = setup
    b = Broker(reg, cfg.action_log)
    assert not b.execute("system", "format_disk", {})[0].allowed
    assert not b.execute("desktop", "read_file", {})[0].allowed                    # missing arg
    assert not b.execute("desktop", "read_file", {"path": 3})[0].allowed           # wrong type
    assert not b.execute("desktop", "read_file", {"path": "x", "evil": 1})[0].allowed
    assert not b.execute("system", "service_control", {"name": "ssh", "action": "kill"})[0].allowed  # enum
    assert not b.execute("system", "read_file", {"path": "~/old.log"})[0].allowed  # wrong agent
    assert calls == []
    assert len(log_entries(cfg)) == 6 and not any(e["allowed"] for e in log_entries(cfg))


def test_read_runs_freely(setup, home):
    cfg, reg, calls = setup
    b = Broker(reg, cfg.action_log)  # deny_all confirmer
    d, r = b.execute("desktop", "read_file", {"path": "~/notes/lab.md"})
    assert d.allowed and r.ok and "diverged" in r.data["text"]
    assert calls == ["read_file"]


def test_delete_needs_confirmation(setup, home):
    cfg, reg, calls = setup
    d, r = Broker(reg, cfg.action_log, deny_all).execute("desktop", "delete_path", {"path": "~/old.log"})
    assert not d.allowed and not r.ok and calls == [] and (home / "old.log").exists()
    asked = []
    yes = lambda *a: asked.append(a) or True  # noqa: E731
    d, r = Broker(reg, cfg.action_log, yes).execute("desktop", "delete_path", {"path": "~/old.log"})
    assert d.allowed and r.ok and asked and calls == ["delete_path"]
    assert not (home / "old.log").exists()
    assert (home / ".local/share/Trash/files/old.log").exists()          # restorable
    assert (home / ".local/share/Trash/info/old.log.trashinfo").exists()


def test_write_new_file_free_but_overwrite_needs_confirmation(setup, home):
    cfg, reg, calls = setup
    b = Broker(reg, cfg.action_log, deny_all)
    d, r = b.execute("desktop", "write_file", {"path": "~/new.txt", "content": "hi"})
    assert d.allowed and r.ok and (home / "new.txt").read_text() == "hi"
    d, r = b.execute("desktop", "write_file", {"path": "~/new.txt", "content": "bye"})
    assert not d.allowed and (home / "new.txt").read_text() == "hi"
    assert Risk.OVERWRITE in d.risks
    d, r = b.execute("desktop", "write_file", {"path": "~/new.txt", "content": "+", "append": True})
    assert d.allowed and (home / "new.txt").read_text() == "hi+"


RISKY_CALLS = [
    ("system", "service_control", {"name": "cron", "action": "stop"}),
    ("system", "install_package", {"name": "htop"}),
    ("desktop", "delete_path", {"path": "~/old.log"}),
    ("desktop", "write_file", {"path": "~/old.log", "content": "x"}),
    ("desktop", "move_path", {"source": "~/notes/lab.md", "destination": "~/old.log"}),
    ("dev", "run_command", {"command": "echo hi"}),
    ("dev", "containers", {"name": "db", "action": "rm"}),
    ("dev", "python_env", {"action": "install", "path": "~/venv", "package": "numpy"}),
    ("research", "arxiv_search", {"query": "transformers"}),
    ("research", "web_search", {"query": "transformers"}),
    ("research", "fetch_page", {"url": "https://example.org"}),
    ("assistant", "forget", {"topic": "project x"}),
]


@pytest.mark.parametrize("agent,tool,args", RISKY_CALLS)
def test_no_risky_tool_runs_unconfirmed(setup, agent, tool, args):
    cfg, reg, calls = setup
    d, r = Broker(reg, cfg.action_log, deny_all).execute(agent, tool, args)
    assert d.risks & CONFIRM, f"{tool} should be risky"
    assert not d.allowed and calls == []
    e = log_entries(cfg)[-1]
    assert e["tool"] == tool and e["allowed"] is False and e["decision"].startswith("denied")


def test_every_tool_with_confirm_risk_is_covered(setup):
    """Every tool that can ever need confirmation appears in RISKY_CALLS."""
    _, reg, _ = setup
    covered = {t for _, t, _ in RISKY_CALLS}
    static_risky = {t.name for t in reg.tools.values() if not callable(t.risk) and t.risk & CONFIRM}
    dynamic = {"write_file", "move_path", "run_command", "containers", "python_env"}
    assert static_risky | dynamic <= covered


def test_failing_risk_function_blocks_call(setup):
    cfg, reg, calls = setup
    reg = Registry()  # every real agent already has its full 8 tools
    reg.add(Tool("boom", "research", "x", {}, [], lambda: "ran", lambda a: 1 / 0))
    d, _ = Broker(reg, cfg.action_log, lambda *a: True).execute("research", "boom", {})
    assert not d.allowed


# ---------- tools ----------

def test_desktop_tools(setup, home):
    cfg, reg, _ = setup
    b = Broker(reg, cfg.action_log, lambda *a: True)
    assert b.execute("desktop", "list_dir", {"path": "~"})[1].data["count"] == 2
    assert b.execute("desktop", "find_files", {"pattern": "lab"})[1].data["matches"][0].endswith("lab.md")
    _, r = b.execute("desktop", "read_file", {"path": "~/nope.txt"})
    assert not r.ok and "no such file" in r.error
    _, r = b.execute("desktop", "move_path", {"source": "~/old.log", "destination": "~/notes"})
    assert r.ok and (home / "notes" / "old.log").exists()


def test_tilde_name_is_rejected_not_guessed(setup, home):
    cfg, reg, _ = setup
    _, r = Broker(reg, cfg.action_log).execute("desktop", "list_dir", {"path": "~notes"})
    assert not r.ok and "~/notes" in r.error
    # a real user's home still works
    _, r = Broker(reg, cfg.action_log).execute("desktop", "list_dir", {"path": "~root"})
    assert r.ok or "Permission" in r.error


def test_research_search_notes(setup):
    cfg, reg, _ = setup
    _, r = Broker(reg, cfg.action_log).execute("research", "search_notes", {"query": "transformer diverged"})
    assert r.ok and r.data["results"][0]["line"] == 2


def test_dev_git_status(setup, home):
    cfg, reg, _ = setup
    repo = home / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.py").write_text("x = 1\n")
    _, r = Broker(reg, cfg.action_log).execute("dev", "git_status", {"repo": "~/proj"})
    assert r.ok and not r.data["clean"] and r.data["changes"][0]["path"] == "a.py"
    _, r = Broker(reg, cfg.action_log).execute("dev", "git_status", {"repo": "~/notes"})
    assert not r.ok and "not a git repository" in r.error


def test_assistant_reminders_and_forget(setup):
    cfg, reg, _ = setup
    b = Broker(reg, cfg.action_log, lambda *a: True)
    _, r = b.execute("assistant", "add_reminder", {"text": "check the training run", "when": "in 2 minutes"})
    assert r.ok and r.data["due"] == "2026-11-14T10:32"
    _, r = b.execute("assistant", "add_reminder", {"text": "x", "when": "sometime soon"})
    assert not r.ok and "could not understand" in r.error
    b.execute("assistant", "remember", {"fact": "Project Falcon uses PyTorch 2.5"})
    b.execute("assistant", "remember", {"fact": "I prefer dark mode"})
    assert b.execute("assistant", "recall", {"query": "falcon"})[1].data["count"] == 1
    assert b.execute("assistant", "forget", {"topic": "project falcon"})[1].data["items_removed"] == 1
    assert b.execute("assistant", "recall", {"query": "falcon"})[1].data["count"] == 0
    everything = b.execute("assistant", "recall", {})[1].data
    assert everything["profile_facts"] == ["I prefer dark mode"]


def test_ics_parse():
    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Group meeting\\, room 2\r\n"
           "DTSTART:20261114T150000\r\nDTEND:20261114T160000\r\nLOCATION:Lab\r\nEND:VEVENT\r\n"
           "BEGIN:VEVENT\r\nSUMMARY:Holiday\r\nDTSTART;VALUE=DATE:20261225\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    ev = parse_ics(ics)
    assert ev[0] == {"title": "Group meeting, room 2", "start": "2026-11-14T15:00",
                     "end": "2026-11-14T16:00", "location": "Lab"}
    assert ev[1]["start"] == "2026-12-25T00:00"


@pytest.mark.parametrize("text,expected", [
    ("in 2 minutes", "2026-11-14T10:32"), ("in 3 hours", "2026-11-14T13:30"),
    ("18:30", "2026-11-14T18:30"), ("6pm", "2026-11-14T18:00"), ("9:00", "2026-11-15T09:00"),
    ("tomorrow 9am", "2026-11-15T09:00"), ("tomorrow at 14:15", "2026-11-15T14:15"),
    ("monday 10am", "2026-11-16T10:00"), ("saturday 10am", "2026-11-21T10:00"),
    ("2026-12-01 08:00", "2026-12-01T08:00"), ("2026-12-01", "2026-12-01T09:00"),
])
def test_parse_when(text, expected):
    assert parse_when(text, NOW).isoformat(timespec="minutes") == expected


@pytest.mark.parametrize("text", ["sometime", "6", "25:00", "tomorrow", "13pm"])
def test_parse_when_rejects(text):
    with pytest.raises(ValueError):
        parse_when(text, NOW)
