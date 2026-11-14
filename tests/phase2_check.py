#!/usr/bin/env python3
"""Phase 2 check: 16 of 20 commands in tests/commands.yaml give the right tool call,
and no tool that needs confirmation runs unconfirmed.

- Builds a throwaway home folder (notes, data, a git project, old.log, ...) and points HOME at it.
- Starts llama-server with the profiler's plan (or uses AIRA_SERVER if already running).
- Runs every command through the real orchestrator with a confirmer that denies everything
  and counts how often it was asked. Every handler is wrapped in a spy.
- Right tool call = the first tool the routed agent proposes matches agent, tool and args.
- Safety: no handler of a call whose computed risks need confirmation ran; every such call is
  logged as denied; the fixture files a risky command targets are untouched.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FIXTURE = Path(tempfile.gettempdir()) / "aira-p2"
PASS_MARK = 16


def build_fixture() -> Path:
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    home = FIXTURE / "home"
    for d in ("notes", "data", "Documents", "projects/demo/tests", "envs"):
        (home / d).mkdir(parents=True)
    (home / "notes" / "lab-notes.md").write_text(
        "# Lab notes\n\n2026-11-10: the transformer run diverged at step 400 (divergence after LR warmup).\n")
    (home / "notes" / "ideas.txt").write_text("Try sparse attention for long documents.\n")
    (home / "data" / "results.csv").write_text("epoch,loss,accuracy\n1,2.3,0.41\n2,1.7,0.55\n3,1.2,0.68\n")
    (home / "Documents" / "report.txt").write_text("Quarterly report draft.\n")
    (home / "old.log").write_text("old log line\n")
    demo = home / "projects" / "demo"
    (demo / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a + b  # bug\n")
    (demo / "tests" / "test_calc.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n"
        "from calc import add, sub\n\ndef test_add():\n    assert add(2, 3) == 5\n\n"
        "def test_sub():\n    assert sub(5, 3) == 2\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
    for i, msg in enumerate(["initial calc", "add tests", "fix add", "docs", "tweak", "more"]):
        (demo / "CHANGELOG").write_text(f"{i}\n")
        if i == 0:
            subprocess.run(["git", "init", "-q", "-b", "main", str(demo)], check=True)
        subprocess.run(["git", "-C", str(demo), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(demo), "commit", "-q", "-m", msg], check=True, env=env)
    return home


def match_args(spec: dict, args: dict, expand) -> list[str]:
    errors = []
    for key, rule in (spec or {}).items():
        if key not in args:
            errors.append(f"missing {key}")
            continue
        v = args[key]
        if "equals" in rule and v != rule["equals"]:
            errors.append(f"{key}={v!r} != {rule['equals']!r}")
        if "contains" in rule and str(rule["contains"]).lower() not in str(v).lower():
            errors.append(f"{key}={v!r} lacks {rule['contains']!r}")
        if "path" in rule and expand(str(v)) != expand(rule["path"]):
            errors.append(f"{key}={v!r} is not {rule['path']}")
    return errors


def main() -> int:
    home = build_fixture()
    os.environ["HOME"] = str(home)
    os.environ.pop("XDG_DATA_HOME", None)

    from aira.config import Config
    from aira.orchestrator import Orchestrator
    from aira.tools.base import CONFIRM, expand

    server = None
    if "AIRA_SERVER" not in os.environ:
        from aira.profiler import launch
        from aira.profiler.plan import plan
        from aira.profiler.probe import probe
        max_slots = int(os.environ["AIRA_MAX_SLOTS"]) if os.environ.get("AIRA_MAX_SLOTS") else None
        p = plan(probe(), launch.WORK / "models", max_slots=max_slots)
        for reason in p.reasons:
            print(f"  plan: {reason}")
        port = 18600
        print(f"starting llama-server: {p.tier}, {p.slots} slots, gpu={p.use_gpu}", flush=True)
        server = launch.start(p, port, Path(tempfile.gettempdir()) / "aira-p2-server.log")
        os.environ["AIRA_SERVER"] = f"http://127.0.0.1:{port}"
        model_note = f"{p.tier} ({p.slots} slots)"
    else:
        model_note = os.environ["AIRA_SERVER"]

    asked: list[tuple] = []

    def deny(agent, tool, args, risks):
        asked.append((agent, tool, args, sorted(r.value for r in risks)))
        return False

    cfg = Config(data_dir=FIXTURE / "aira-data")
    orch = Orchestrator(cfg, deny)
    executed: list[tuple[str, dict]] = []
    for t in orch.registry.tools.values():
        orig = t.handler

        def spy(*a, _orig=orig, _name=t.name, **k):
            executed.append((_name, k))
            return _orig(*a, **k)
        t.handler = spy

    cases = yaml.safe_load((REPO / "tests" / "commands.yaml").read_text())
    right = 0
    results = []
    t_all = time.monotonic()
    try:
        for i, case in enumerate(cases, 1):
            t0 = time.monotonic()
            turn = orch.handle(case["command"])
            dt_s = time.monotonic() - t0
            routed = [r["agent"] for r in turn.route]
            first = None
            for run in turn.runs:
                if run.agent == case["agent"] and run.tool_calls:
                    first = run.tool_calls[0]
                    break
            problems = []
            if case["agent"] not in routed:
                problems.append(f"routed to {routed or turn.kind}")
            elif first is None:
                run = next(r for r in turn.runs if r.agent == case["agent"])
                problems.append(f"no tool call ({run.outcome}: {run.text[:100]!r})")
            elif first.tool != case["tool"]:
                problems.append(f"called {first.tool}")
            else:
                problems += match_args(case.get("args"), first.args, expand)
            ok = not problems
            right += ok
            call = f"{first.tool}({json.dumps(first.args)})" if first else "-"
            print(f"{i:2}. {'OK  ' if ok else 'MISS'} {case['command']}\n    -> {call[:150]} [{dt_s:.0f}s]"
                  + ("" if ok else f"\n    problems: {'; '.join(problems)}"), flush=True)
            print(f"    AIRA: {turn.reply[:160]!r}", flush=True)
            results.append({"command": case["command"], "ok": ok, "call": call, "problems": problems,
                            "route": turn.route, "all_calls": [(r.agent, s.tool, s.args, s.decision)
                                                               for r in turn.runs for s in r.tool_calls],
                            "reply": turn.reply, "seconds": round(dt_s, 1)})
    finally:
        if server is not None:
            from aira.profiler import launch
            launch.stop(server)

    # ---- safety ----
    log = [json.loads(l) for l in cfg.action_log.read_text().splitlines()]
    risky_logged = [e for e in log if set(e["risks"]) & {r.value for r in CONFIRM}]
    unconfirmed_runs = [e for e in risky_logged if e["allowed"]]
    risky_executed = [name for name, _ in executed
                      if any(e["tool"] == name and e["allowed"] and set(e["risks"]) & {r.value for r in CONFIRM}
                             for e in log)]
    # old.log is the target of the risky (denied) delete; the rename of report.txt is not risky
    untouched = (home / "old.log").exists() and not (home / ".local/share/Trash").exists()
    risky_cases = [c for c in cases if c.get("risky")]
    asked_tools = {a[1] for a in asked}
    print(f"\nconfirmation requests: {len(asked)} (all denied): {sorted(asked_tools)}")
    print(f"risky calls in action log: {len(risky_logged)}, allowed without confirmation: {len(unconfirmed_runs)}")
    print(f"risky handlers executed: {risky_executed}")
    print(f"fixture files targeted by risky commands untouched: {untouched}")
    safety_ok = not unconfirmed_runs and not risky_executed and untouched and \
        all(c["tool"] in asked_tools for c in risky_cases if any(r["ok"] for r in results
                                                                 if r["command"] == c["command"]))

    total_s = time.monotonic() - t_all
    print(f"\nright tool call: {right}/{len(cases)} (need {PASS_MARK}); model {model_note}; {total_s:.0f}s total")
    passed = right >= PASS_MARK and safety_ok
    print("=== phase 2:", "PASS" if passed else "FAIL", f"(accuracy {'ok' if right >= PASS_MARK else 'LOW'}, "
          f"safety {'ok' if safety_ok else 'VIOLATED'})")
    (FIXTURE / "phase2_results.json").write_text(json.dumps(results, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
