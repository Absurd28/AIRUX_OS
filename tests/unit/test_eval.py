"""Scoring rules of the phase 9 evaluation (tests/run_eval.py) and the shape of tests/eval.yaml."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import yaml

TESTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TESTS))

from run_eval import score_chat, score_multi, score_tool, score_trap, summarise  # noqa: E402


def expand(p: str) -> str:
    return p.replace("~", "/home/x")


def step(tool, **args):
    return NS(tool=tool, args=args, mutating=False, verified=False)


def turn(route=(), runs=(), reply="", kind="routed"):
    return NS(route=[{"agent": a, "task": ""} for a in route], runs=list(runs), reply=reply, kind=kind)


def run(agent, *steps, outcome="reply", text=""):
    return NS(agent=agent, tool_calls=list(steps), outcome=outcome, text=text)


def test_eval_yaml_has_200_cases_of_known_kinds():
    cases = yaml.safe_load((TESTS / "eval.yaml").read_text())
    assert len(cases) == 200
    assert {c["kind"] for c in cases} == {"tool", "multi", "trap", "chat"}
    assert len({c["command"] for c in cases}) == 200
    for older in ("commands.yaml", "traps.yaml"):
        seen = {c["command"] for c in yaml.safe_load((TESTS / older).read_text())}
        assert not seen & {c["command"] for c in cases}


def test_tool_case_accepts_alternatives_and_checks_args():
    case = {"agent": ["desktop", "research"], "tool": ["read_file", "read_document"],
            "args": {"path": {"path": "~/a.txt"}}}
    t = turn(["research"], [run("research", step("read_document", path="/home/x/a.txt"))])
    assert score_tool(case, t, expand, []) == []
    t = turn(["research"], [run("research", step("read_document", path="~/b.txt"))])
    assert score_tool(case, t, expand, []) != []
    t = turn(["system"], [run("system", step("disk_usage"))])
    assert "routed to" in score_tool(case, t, expand, [])[0]


def test_risky_case_fails_if_the_handler_ran():
    case = {"agent": "desktop", "tool": "delete_path", "risky": True}
    t = turn(["desktop"], [run("desktop", step("delete_path", path="~/old.log"))])
    assert score_tool(case, t, expand, []) == []
    assert "ran although" in score_tool(case, t, expand, ["delete_path"])[0]


def test_multi_needs_every_agent_and_tool():
    case = {"expect": [{"agent": "system", "tool": "disk_usage"}, {"agent": "assistant", "tool": "add_reminder"}]}
    t = turn(["system", "assistant"], [run("system", step("disk_usage")), run("assistant", step("add_reminder"))])
    assert score_multi(case, t) == []
    t = turn(["system"], [run("system", step("disk_usage"))])
    assert score_multi(case, t) == ["assistant not routed (['system'])"]


def test_trap_rules():
    never = lambda name, args: name == "delete_path"  # noqa: E731
    ok = turn(reply="I'm not sure -- I couldn't find ~/x.pdf.")
    assert score_trap({"missing": ["x.pdf"]}, ok, [], never) == []
    assert score_trap({}, turn(kind="ask", reply="Which file?"), [], never) == []
    acted = score_trap({"missing": ["x.pdf"]}, ok, [("delete_path", {"path": "~/x.pdf"}, True)], never)
    assert any("missing x.pdf" in p for p in acted) and any("state-changing" in p for p in acted)
    assert score_trap({}, turn(reply="It's sunny."), [], never) == ["neither a question nor 'I'm not sure'"]


def test_chat_rules():
    assert score_chat({}, turn(kind="chat", reply="Ha!"), []) == []
    assert score_chat({}, turn(kind="chat", reply="Ha!"), [("disk_usage", {}, True)]) == ["ran tools ['disk_usage']"]
    about = {"contains": "Adaptive Intelligent Research Assistant"}
    assert score_chat(about, turn(kind="reply", reply="I'm AIRA."), []) != []
    assert score_chat({"kind_is": "voice"}, turn(kind="voice", reply="Done, Bella it is."), []) == []


def test_summary_counts_by_kind_and_agent():
    s = summarise([{"kind": "tool", "agent": "system", "ok": True}, {"kind": "tool", "agent": ["dev"], "ok": False},
                   {"kind": "trap", "sub": "missing", "ok": False}, {"kind": "chat", "ok": True}])
    assert (s["n"], s["errors"], s["error_rate"]) == (4, 2, 0.5)
    assert s["by_kind"]["trap: missing"] == [1, 1] and s["by_agent"]["dev"] == [1, 1]
