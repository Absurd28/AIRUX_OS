"""Unit tests for the grounding guard, using a scripted fake model (no server)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from aira import guard
from aira.config import Config
from aira.llm import Completion
from aira.orchestrator import Orchestrator
from aira.memory.embed import HashEmbedder
from aira.memory.vault import Vault


class FakeLLM:
    """Returns scripted JSON steps. `script` items are dicts, or callables(messages, seed, temperature)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def total_slots(self):
        return 3

    def chat(self, messages, schema=None, *, slot=None, temperature=0.0, max_tokens=512, seed=None):
        self.calls.append({"messages": messages, "seed": seed, "temperature": temperature, "schema": schema})
        if not self.script:
            raise AssertionError("fake model ran out of script")
        item = self.script.pop(0)
        data = item(messages, seed, temperature) if callable(item) else item
        return Completion(content="", data=data, prompt_tokens=0, cached_tokens=0, gen_tokens=0, seconds=0)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "notes").mkdir(parents=True)
    (h / "notes" / "lab-notes.md").write_text("step 400 diverged\n")
    (h / "report.txt").write_text("draft\n")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return h


def make(home: Path, tmp_path: Path, script, confirm=lambda *a: True):
    cfg = Config(server_url="http://fake", data_dir=tmp_path / "data")
    llm = FakeLLM(script)
    vault = Vault(cfg.data_dir / "vault.db", "test-key", embedder=HashEmbedder(),
                  clock=lambda: dt.datetime(2026, 11, 14, 10, 0))
    orch = Orchestrator(cfg, confirm, llm=llm, vault=vault)
    executed = []
    for t in orch.registry.tools.values():
        orig = t.handler

        def spy(*a, _o=orig, _n=t.name, **k):
            executed.append(_n)
            return _o(*a, **k)
        t.handler = spy
    return orch, llm, executed


def route(agent, task):
    return {"route": [{"agent": agent, "task": task}]}


def call(tool, **args):
    return {"tool": tool, "args": args}


def reply(text):
    return {"tool": "reply", "args": {"text": text}}


# ---------- layer 2: entity check ----------

def test_missing_file_is_never_acted_on(home, tmp_path):
    orch, llm, executed = make(home, tmp_path, [route("desktop", "delete ~/reprt.txt"),
                                                call("delete_path", path="~/reprt.txt")])
    turn = orch.handle("Delete ~/reprt.txt")
    assert executed == [] and (home / "report.txt").exists()
    assert turn.kind == "ask"
    assert "couldn't find" in turn.reply and turn.reply.endswith("?")
    assert "~/report.txt" in turn.reply  # close-match suggestion, as a question


def test_tilde_name_entity_missing(home, tmp_path):
    orch, _, executed = make(home, tmp_path, [route("desktop", "list notes"), call("list_dir", path="~notes")])
    turn = orch.handle("What's in my notes?")
    assert executed == [] and turn.kind == "ask" and "~/notes" in turn.reply


# ---------- layer 5: agreement ----------

def test_risky_call_samples_disagree_asks(home, tmp_path):
    script = [route("desktop", "delete the report"), call("delete_path", path="~/report.txt"),
              call("delete_path", path="~/notes"), call("delete_path", path="~/notes/lab-notes.md")]
    orch, llm, executed = make(home, tmp_path, script)
    turn = orch.handle("Delete the report")
    assert executed == [] and turn.kind == "ask" and "not sure" in turn.reply
    seeds = [c["seed"] for c in llm.calls[2:]]
    assert seeds == [1000, 1001] and all(c["temperature"] == 0.7 for c in llm.calls[2:])


def test_risky_call_two_of_three_agree_then_asks_user(home, tmp_path):
    asked = []
    script = [route("desktop", "delete report"), call("delete_path", path="~/report.txt"),
              call("delete_path", path="~/notes"), call("delete_path", path="~/report.txt"),
              reply("Deleted ~/report.txt.")]
    orch, _, executed = make(home, tmp_path, script, confirm=lambda *a: asked.append(a) or True)
    turn = orch.handle("Delete ~/report.txt")
    assert len(asked) == 1 and executed == ["delete_path"]
    assert not (home / "report.txt").exists()
    assert "Verified:" in turn.reply and "Deleted ~/report.txt." in turn.reply


# ---------- layer 6: read-back ----------

def test_done_requires_readback(home, tmp_path, monkeypatch):
    script = [route("desktop", "write"), call("write_file", path="~/a.txt", content="hi"), reply("Done, created ~/a.txt.")]
    orch, _, _ = make(home, tmp_path, script)
    orch.registry.get("write_file").readback = lambda a, r: (False, "file content differs")
    turn = orch.handle("Create ~/a.txt saying hi")
    assert "Done" not in turn.reply and "Not verified" in turn.reply and "not sure" in turn.reply.lower()


def test_done_with_readback_passes(home, tmp_path):
    script = [route("desktop", "write"), call("write_file", path="~/a.txt", content="hi"), reply("Created ~/a.txt.")]
    orch, _, _ = make(home, tmp_path, script)
    turn = orch.handle("Create ~/a.txt saying hi")
    assert turn.reply.startswith("Created ~/a.txt.") and "Verified:" in turn.reply


def test_shell_command_never_verified(home, tmp_path):
    script = [route("dev", "run echo"), call("run_command", command="echo hi"), call("run_command", command="echo hi"),
              call("run_command", command="echo hi"), reply("Done.")]
    orch, _, executed = make(home, tmp_path, script)
    turn = orch.handle("run echo hi")
    assert executed == ["run_command"] and "Done." not in turn.reply and "Not verified" in turn.reply


# ---------- layers 3 and 4 ----------

def test_reply_without_tool_result_is_replaced(home, tmp_path):
    orch, _, _ = make(home, tmp_path, [route("system", "cpu temp"), reply("Your CPU is at 54°C.")])
    turn = orch.handle("What's my CPU temperature?")
    assert "54" not in turn.reply and turn.reply.startswith("I'm not sure")


def test_failed_tool_reported_not_filled_in(home, tmp_path):
    orch, _, _ = make(home, tmp_path, [route("dev", "git status"), call("git_status", repo="~/notes"),
                                       reply("The repo is clean on main.")])
    turn = orch.handle("git status of ~/notes")
    # ~/notes exists but is not a repo -> entity check blocks (it is the git repository that is missing)
    assert "clean" not in turn.reply and turn.kind == "ask"


def test_failure_status_line(home, tmp_path):
    orch, _, _ = make(home, tmp_path, [route("desktop", "clipboard"), call("clipboard", action="get"),
                                       reply("Your clipboard says hello.")])
    orch.registry.get("clipboard").handler = lambda **k: (_ for _ in ()).throw(guard.ToolError("no clipboard tool"))
    turn = orch.handle("What's on my clipboard?")
    assert "hello" not in turn.reply and turn.reply.startswith("I couldn't verify that: clipboard failed")


def test_denied_call_status(home, tmp_path):
    script = [route("research", "arxiv"), call("arxiv_search", query="x"), call("arxiv_search", query="x"),
              call("arxiv_search", query="x"), reply("I found 5 papers.")]
    orch, _, executed = make(home, tmp_path, script, confirm=lambda *a: False)
    turn = orch.handle("search arxiv for x")
    assert executed == [] and "5 papers" not in turn.reply and "Not done" in turn.reply


# ---------- layer 7 ----------

def test_reply_checker_regenerates_then_passes(home, tmp_path):
    script = [route("system", "disk"), call("disk_usage", path="~"), reply("You have 999 GB free."),
              lambda m, s, t: reply("Free space: " + str(__import__("json").loads(m[-3]["content"].split("\n", 1)[1])
                                                         ["data"]["free_gib"]) + " GiB.")]
    orch, llm, _ = make(home, tmp_path, script)
    turn = orch.handle("How much disk space is free?")
    assert "999" not in turn.reply and "GiB" in turn.reply
    assert any("not in the tool results" in c["messages"][-1]["content"] for c in llm.calls[-1:])


def test_reply_checker_falls_back_to_tool_output(home, tmp_path):
    script = [route("system", "disk"), call("disk_usage", path="~"), reply("999 GB free."), reply("Still 999 GB.")]
    orch, _, _ = make(home, tmp_path, script)
    turn = orch.handle("How much disk space is free?")
    assert "999" not in turn.reply and "Here is what disk_usage returned" in turn.reply


def test_check_reply_rules(home):
    ev = guard.Evidence()
    ev.add("Show rows in ~/data/results.csv")
    ev.add({"path": str(home / "data/results.csv"), "rows": 3, "columns": ["epoch", "loss"], "free_gib": 4.36,
            "matches": ["a", "b"]})
    assert guard.check_reply("It has 3 rows and 2 columns.", ev) == []          # 2 = list length
    assert guard.check_reply("~/data/results.csv has 3 rows.", ev) == []
    assert guard.check_reply("4.4 GiB free", ev) == []                         # rounding allowed
    assert "number 4 is not in the tool results" in guard.check_reply("It has 4 rows.", ev)
    assert any("GiB, not GB" in p for p in guard.check_reply("4.36 GB free", ev))
    assert any("name other.csv" in p for p in guard.check_reply("See other.csv", ev))
    assert any("path ~/data/x" in p for p in guard.check_reply("Saved to ~/data/x", ev))


def test_claims_done():
    assert guard.claims_done("Done. I deleted it.")
    assert not guard.claims_done("I couldn't delete it.")
    assert not guard.claims_done("The file was not deleted.")
    assert not guard.claims_done("I'm not sure. I stopped after 4 steps without finishing.")
    assert not guard.claims_done("I don't see any events scheduled for tomorrow.")
    assert not guard.claims_done("The reminder isn’t set yet.")
    assert not guard.claims_done("Nothing was deleted.")
    assert guard.claims_done("Your meeting is scheduled for 4pm.")


@pytest.mark.parametrize("text,abstains", [
    ("The GPU fan speed is not available in the current system information.", True),
    ("The tool results do not include the CPU temperature.", True),
    ("I couldn't find any line mentioning 3e-4.", True),
    ("There is no information about project Falcon.", True),
    ("I'm not sure. The fan speed is not shown.", True),         # already phrased: unchanged
    ("The repository is clean; there are no uncommitted changes.", False),
    ("The cron service is running.", False),
])
def test_normalise_abstention(text, abstains):
    out = guard.normalise_abstention(text)
    assert out.startswith("I'm not sure") == abstains
    if text.startswith("I'm not sure"):
        assert out == text


def test_unavailable_reply_gets_not_sure(home, tmp_path):
    orch, _, _ = make(home, tmp_path, [route("system", "fan"), call("gpu_status"),
                                       reply("The GPU fan speed is not available in the current system information.")])
    orch.registry.get("gpu_status").handler = lambda: {"gpus": [{"name": "RTX", "temperature_c": "50"}]}
    assert orch.handle("What's my GPU fan speed?").reply.startswith("I'm not sure. The GPU fan speed is not available")


def test_blocked_call_not_reported_as_failed(home, tmp_path):
    orch, _, _ = make(home, tmp_path, [route("assistant", "remind later"),
                                       call("add_reminder", text="it", when="later")])
    turn = orch.handle("Remind me about it later")
    assert "unknown error" not in turn.reply and "failed" not in turn.reply
    assert turn.reply.startswith("I couldn't understand the time")


def test_forget_is_not_recorded_as_episode(home, tmp_path):
    script = [route("assistant", "remember"), call("remember", fact="Project Falcon uses PyTorch"),
              reply("Remembered."),
              route("assistant", "forget falcon"), call("forget", topic="project falcon"),
              call("forget", topic="project falcon"), call("forget", topic="project falcon"),
              reply("Forgot project Falcon.")]
    orch, _, _ = make(home, tmp_path, script)
    orch.handle("Remember that project Falcon uses PyTorch")
    turn = orch.handle("Forget everything about project Falcon")
    assert "Verified: nothing about it remains" in turn.reply
    assert orch.vault.topic_present("project falcon") == {"keyword": [], "vector": []}
    assert [e.text for e in orch.vault.by_kind("episode")] == ["Forgot a topic on request."]
    # the action log keeps the audit record but not the forgotten content, and is only in the vault
    assert orch.vault.keyword_search("falcon") == []
    log = orch.action_log()
    assert [a["tool"] for a in log] == ["remember", "forget"]
    assert log[0]["args"] == {"fact": "[forgotten]"} and log[1]["args"] == {"topic": "[forgotten]"}
    assert not orch.config.action_log.exists()


def test_abbreviations_are_not_file_names():
    ev = guard.Evidence()
    ev.add({"x": 1})
    assert set(guard.check_reply("Give a path, e.g. ~/notes, i.e. a folder, by 9 a.m. tomorrow.", ev)) == {
        "path ~/notes is not in the tool results", "number 9 is not in the tool results"}


def test_number_with_unit_is_not_a_file_name():
    ev = guard.Evidence()
    ev.add({"power_w": "32.67"})
    assert guard.check_reply("Power draw is 32.67W.", ev) == []


def test_vague_time_is_asked_before_calling(home, tmp_path):
    orch, _, executed = make(home, tmp_path, [route("assistant", "remind later"),
                                              call("add_reminder", text="it", when="later")])
    turn = orch.handle("Remind me about it later")
    assert executed == [] and turn.kind == "ask" and "time" in turn.reply and turn.reply.endswith("?")


def test_same_tool_failing_twice_stops(home, tmp_path):
    script = [route("assistant", "dentist"), call("agenda", day="next week"),
              call("agenda", day="this week"), call("agenda", day="soon")]
    orch, llm, executed = make(home, tmp_path, script)
    turn = orch.handle("When is my dentist appointment?")
    assert executed == ["agenda", "agenda"] and len(llm.script) == 1
    assert turn.reply.startswith("I'm not sure") and "failed twice" in turn.reply


def test_fallback_says_not_sure(home, tmp_path):
    script = [route("system", "disk"), call("disk_usage", path="~"), reply("999 GB free."), reply("Still 999 GB.")]
    orch, _, _ = make(home, tmp_path, script)
    assert orch.handle("disk?").reply.startswith("I'm not sure how to answer that exactly")


# ---------- router never answers in free text ----------

def test_router_unsupported_and_chat(home, tmp_path):
    orch, _, _ = make(home, tmp_path, [{"unsupported": True}, {"chat": "about"}])
    assert orch.handle("What's the weather in Pune?").reply.startswith("I'm not sure")
    about = orch.handle("Who are you?").reply
    assert "Adaptive Intelligent Research Assistant" in about
    assert "chat" in str(orch.router_schema) and "reply" not in str(orch.router_schema)
