"""Permission broker: the only path from a proposed tool call to a handler.

- Unknown tools and arguments that fail the tool's schema are rejected.
- Risk is computed in code (Tool.risks); anything in CONFIRM runs only after the
  confirm callback returns True. Read-only and plain writes run freely.
- Every request, decision and outcome is appended to the action log (JSON lines).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .tools.base import Registry, Risk, ToolError, ToolResult

# confirm(agent, tool_name, args, risks) -> bool
Confirmer = Callable[[str, str, dict, set[Risk]], bool]


def deny_all(agent: str, tool: str, args: dict, risks: set[Risk]) -> bool:
    return False


@dataclass
class Decision:
    allowed: bool
    reason: str
    risks: set[Risk]


class Broker:
    def __init__(self, registry: Registry, log_path: Path, confirm: Confirmer = deny_all):
        self.registry = registry
        self.log_path = log_path
        self.confirm = confirm
        self._lock = threading.Lock()

    # ----- validation (mirrors the grammar; the broker never trusts the caller) -----
    def validate(self, tool_name: str, args: dict) -> str | None:
        tool = self.registry.get(tool_name)
        if tool is None:
            return f"unknown tool {tool_name!r}"
        if not isinstance(args, dict):
            return "arguments must be an object"
        extra = set(args) - set(tool.params)
        if extra:
            return f"unexpected arguments {sorted(extra)}"
        missing = [k for k in tool.required if k not in args]
        if missing:
            return f"missing arguments {missing}"
        for k, v in args.items():
            spec = tool.params[k]
            t = spec.get("type")
            if t == "string" and not isinstance(v, str):
                return f"{k} must be a string"
            if t == "integer" and (not isinstance(v, int) or isinstance(v, bool)):
                return f"{k} must be an integer"
            if t == "boolean" and not isinstance(v, bool):
                return f"{k} must be true or false"
            if "enum" in spec and v not in spec["enum"]:
                return f"{k} must be one of {spec['enum']}"
            if t == "integer":
                if "minimum" in spec and v < spec["minimum"]:
                    return f"{k} must be >= {spec['minimum']}"
                if "maximum" in spec and v > spec["maximum"]:
                    return f"{k} must be <= {spec['maximum']}"
        return None

    def decide(self, agent: str, tool_name: str, args: dict) -> Decision:
        err = self.validate(tool_name, args)
        if err:
            return Decision(False, f"rejected: {err}", set())
        tool = self.registry.get(tool_name)
        assert tool is not None
        if tool.agent != agent:
            return Decision(False, f"rejected: {tool_name} belongs to the {tool.agent} agent, not {agent}", set())
        try:
            risks = tool.risks(args)
        except Exception as exc:  # a risk function that fails must not let the call through
            return Decision(False, f"rejected: could not assess risk: {exc}", {Risk.EXEC})
        if not tool.needs_confirmation(args):
            return Decision(True, "auto: no confirmation needed", risks)
        if self.confirm(agent, tool_name, args, risks):
            return Decision(True, "confirmed by user", risks)
        return Decision(False, "denied: user did not confirm", risks)

    def execute(self, agent: str, tool_name: str, args: dict) -> tuple[Decision, ToolResult]:
        t0 = time.time()
        decision = self.decide(agent, tool_name, args)
        if not decision.allowed:
            result = ToolResult(False, error=decision.reason)
        else:
            tool = self.registry.get(tool_name)
            assert tool is not None
            try:
                result = ToolResult(True, data=tool.handler(**args))
            except ToolError as exc:
                result = ToolResult(False, error=str(exc))
            except Exception as exc:  # report, never hide
                result = ToolResult(False, error=f"{type(exc).__name__}: {exc}")
        self._log({"ts": t0, "agent": agent, "tool": tool_name, "args": args,
                   "risks": sorted(r.value for r in decision.risks), "allowed": decision.allowed,
                   "decision": decision.reason, "ok": result.ok, "error": result.error or None,
                   "ms": round((time.time() - t0) * 1000)})
        return decision, result

    def _log(self, entry: dict) -> None:
        line = json.dumps(entry, default=str)
        with self._lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(line + "\n")
