"""Sub-agents: one system prompt and up to 8 tools each, all served by the same model.

Each step is a single JSON object constrained by a schema built from the agent's tools:
exactly one of its tools with valid arguments, or `reply`, or `ask_user`.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from .broker import Broker
from .llm import LLM, LLMError
from .tools.base import Registry, ToolResult

PERSONA = ("You are part of AIRA (Adaptive Intelligent Research Assistant), the assistant built into AIRUX. "
           "Be calm, brief and direct. Never guess: if a file, app, service, container, event or fact "
           "is not confirmed by a tool result, say you are not sure or ask. Never invent tool results.")

ROLES = {
    "system": "You are AIRA's System agent: settings, power, network, services, packages, GPU and disk health.",
    "desktop": "You are AIRA's Desktop agent: files and folders, applications and the clipboard.",
    "dev": "You are AIRA's Dev agent: git, tests, terminal commands, Podman containers, Python environments.",
    "research": "You are AIRA's Research agent: local notes and papers, datasets, and arXiv search.",
    "assistant": "You are AIRA's Assistant agent: reminders, calendar events, and facts remembered about the user.",
}

RULES = """Rules:
- Answer with one JSON object: {"tool": <name>, "args": {...}}.
- Use a tool to act or to look something up. After a tool result, either call another tool or finish.
- Finish with {"tool": "reply", "args": {"text": ...}}: a short answer based only on the tool results.
- If the request is unclear, a needed detail is missing, or something does not exist,
  use {"tool": "ask_user", "args": {"question": ...}} instead of guessing.
- If a tool fails or is denied, say so plainly in the reply. Do not retry the same call.
- Paths: use exactly what the user said. The home folder is ~ and things in it are ~/name
  (for example ~/notes, ~/old.log), never ~name. Do not invent paths."""

META = {
    "reply": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
              "additionalProperties": False},
    "ask_user": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"],
                 "additionalProperties": False},
}


@dataclass
class Step:
    tool: str
    args: dict
    decision: str = ""
    result: dict | None = None


@dataclass
class AgentRun:
    agent: str
    task: str
    steps: list[Step] = field(default_factory=list)
    outcome: str = ""        # "reply", "ask", "error", "step_limit"
    text: str = ""

    @property
    def tool_calls(self) -> list[Step]:
        return [s for s in self.steps if s.tool not in META]


class SubAgent:
    def __init__(self, name: str, registry: Registry, llm: LLM, broker: Broker, slot: int | None,
                 max_steps: int = 4, user_name: str = ""):
        self.name = name
        self.tools = registry.for_agent(name)
        assert len(self.tools) <= 8, f"{name} has {len(self.tools)} tools"
        self.llm, self.broker, self.slot, self.max_steps = llm, broker, slot, max_steps
        self.user_name = user_name
        self.schema = self._schema()
        self.system_prompt = self._prompt()

    def _schema(self) -> dict:
        options = [{"type": "object",
                    "properties": {"tool": {"const": t.name}, "args": t.args_schema()},
                    "required": ["tool", "args"], "additionalProperties": False} for t in self.tools]
        options += [{"type": "object", "properties": {"tool": {"const": n}, "args": s},
                     "required": ["tool", "args"], "additionalProperties": False} for n, s in META.items()]
        return {"oneOf": options}

    def _prompt(self) -> str:
        tools = "\n".join("- " + t.signature() for t in self.tools)
        who = f" The user's name is {self.user_name}." if self.user_name else ""
        return f"{PERSONA}{who}\n{ROLES[self.name]}\n\nTools:\n{tools}\n- reply(text)\n- ask_user(question)\n\n{RULES}"

    def propose(self, messages: list[dict], seed: int | None = None, temperature: float = 0.0) -> dict:
        """One constrained model step (no execution)."""
        c = self.llm.chat(messages, self.schema, slot=self.slot, temperature=temperature,
                          max_tokens=400, seed=seed)
        return c.data or {}

    def first_messages(self, task: str, now: dt.datetime | None = None) -> list[dict]:
        now = now or dt.datetime.now()
        # the time goes in the user turn so the system prompt stays identical and cached
        return [{"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"[{now:%A %Y-%m-%d %H:%M}] {task}"}]

    def run(self, task: str) -> AgentRun:
        out = AgentRun(self.name, task)
        messages = self.first_messages(task)
        for _ in range(self.max_steps):
            try:
                step = self.propose(messages)
            except LLMError as exc:
                out.outcome, out.text = "error", f"I couldn't reach the model: {exc}"
                return out
            tool, args = step.get("tool", ""), step.get("args", {})
            if tool == "reply":
                out.steps.append(Step(tool, args))
                out.outcome, out.text = "reply", args.get("text", "")
                return out
            if tool == "ask_user":
                out.steps.append(Step(tool, args))
                out.outcome, out.text = "ask", args.get("question", "")
                return out
            decision, result = self.broker.execute(self.name, tool, args)
            out.steps.append(Step(tool, args, decision.reason, result.to_dict()))
            messages.append({"role": "assistant", "content": json.dumps(step)})
            messages.append({"role": "user", "content": _result_message(tool, decision.reason, result)})
        out.outcome = "step_limit"
        out.text = "I stopped after several steps without finishing. " + _summary(out)
        return out


def _result_message(tool: str, decision: str, result: ToolResult) -> str:
    body = json.dumps(result.to_dict(), default=str)
    if len(body) > 6000:
        body = body[:6000] + "...(truncated)"
    return f"TOOL RESULT for {tool} ({decision}):\n{body}"


def _summary(run: AgentRun) -> str:
    parts = [f"{s.tool}: {'ok' if (s.result or {}).get('ok') else (s.result or {}).get('error', '')}"
             for s in run.tool_calls]
    return "Steps: " + "; ".join(parts) if parts else ""
