"""Text interface: python -m aira [--once "command"] [--json] [--yes-to-nothing]

Risky actions are shown with their computed risks and run only after you type 'yes'.
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import Config
from .orchestrator import Orchestrator, turn_to_dict
from .tools.base import Risk


def tty_confirm(agent: str, tool: str, args: dict, risks: set[Risk]) -> bool:
    print(f"\n  AIRA wants to run {agent}.{tool}")
    for k, v in args.items():
        s = str(v)
        print(f"    {k}: {s if len(s) < 300 else s[:300] + '...'}")
    print(f"    risk: {', '.join(sorted(r.value for r in risks))}")
    try:
        answer = input("  Proceed? type 'yes' to confirm: ").strip().lower()
    except EOFError:
        answer = ""
    return answer == "yes"


def deny(agent: str, tool: str, args: dict, risks: set[Risk]) -> bool:
    print(f"  (denied without asking: {agent}.{tool} {args})")
    return False


def show(turn, as_json: bool) -> None:
    if as_json:
        print(json.dumps(turn_to_dict(turn), indent=2))
        return
    for run in turn.runs:
        for s in run.tool_calls:
            status = "ok" if (s.result or {}).get("ok") else f"failed: {(s.result or {}).get('error')}"
            print(f"  [{run.agent}] {s.tool}({json.dumps(s.args)[:160]}) -> {status}")
    print(f"AIRA: {turn.reply}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aira")
    ap.add_argument("--once", help="run one command and exit")
    ap.add_argument("--json", action="store_true", help="print the full turn as JSON")
    ap.add_argument("--deny-all", action="store_true", help="refuse every risky action without asking")
    args = ap.parse_args(argv)

    confirm = deny if args.deny_all else tty_confirm
    orch = Orchestrator(Config(), confirm)
    if args.once:
        show(orch.handle(args.once), args.json)
        return 0
    print("AIRA (Adaptive Intelligent Research Assistant). Type a command, or 'quit'.")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.lower() in ("quit", "exit"):
            return 0
        if line:
            show(orch.handle(line), args.json)


if __name__ == "__main__":
    sys.exit(main())
