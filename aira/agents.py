"""Sub-agents: one system prompt and up to 8 tools each, all served by the same model.

Each step is a single JSON object constrained by a schema built from the agent's tools
(grounding layer 1). Every step then passes the grounding guard (aira.guard) in this order:
entity check (2) -> agreement for risky calls (5) -> permission broker -> read-back (6);
the final reply goes through facts-only (3), failure notes (4) and the reply checker (7).
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from . import citations, guard
from .broker import Broker
from .llm import LLM, LLMError
from .tools.base import Registry, ToolResult

PERSONA = ("You are part of AIRA (Adaptive Intelligent Research Assistant), the assistant built into AIRUX. "
           "Be calm, brief and direct. Never guess: if a file, app, service, container, event or fact "
           "is not confirmed by a tool result, say you are not sure or ask. Never invent tool results. "
           "\"I'm not sure\" is a correct answer when the tools do not show the answer.")

ROLES = {
    "system": "You are AIRA's System agent: settings, power, network, services, packages, GPU and disk health.",
    "desktop": "You are AIRA's Desktop agent: files and folders, applications and the clipboard.",
    "dev": "You are AIRA's Dev agent: git, tests, terminal commands, Podman containers, Python environments, "
           "watching running jobs.",
    "research": "You are AIRA's Research agent: local notes and papers, document summaries with citations, "
                "datasets (row and column counts), arXiv and web search, and running Jupyter notebooks.",
    "assistant": "You are AIRA's Assistant agent: reminders, calendar events, and facts remembered about the user.",
}

RULES = """Rules:
- Answer with one JSON object: {"tool": <name>, "args": {...}}.
- Use a tool to act or to look something up. After a tool result, either call another tool or finish.
- Finish with {"tool": "reply", "args": {"text": ...}}: a short answer using only values that appear in
  the tool results, with the same units (GiB stays GiB). If the results do not answer the question,
  reply "I'm not sure" and say what you could not find.
- If the request is unclear, a needed detail is missing, or something does not exist,
  use {"tool": "ask_user", "args": {"question": ...}} instead of guessing.
- If no tool can answer the question, reply "I'm not sure" rather than answering from memory.
- If a tool fails or is denied, say so plainly in the reply. Do not retry the same call.
- Paths: use exactly what the user said. The home folder is ~ and things in it are ~/name
  (for example ~/notes, ~/old.log), never ~name. Do not invent paths."""

META = {
    "reply": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
              "additionalProperties": False},
    "ask_user": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"],
                 "additionalProperties": False},
}
REPLY_ONLY = {"type": "object", "properties": {"tool": {"const": "reply"}, "args": META["reply"]},
              "required": ["tool", "args"], "additionalProperties": False}
AGREEMENT_SAMPLES = 3


@dataclass
class Step:
    tool: str
    args: dict
    decision: str = ""
    result: dict | None = None
    mutating: bool = False
    verified: bool = False
    readback: str = ""


@dataclass
class AgentRun:
    agent: str
    task: str
    steps: list[Step] = field(default_factory=list)
    outcome: str = ""        # "reply", "ask", "error", "step_limit"
    text: str = ""
    guard_events: list[str] = field(default_factory=list)

    @property
    def tool_calls(self) -> list[Step]:
        return [s for s in self.steps if s.tool not in META]


class SubAgent:
    def __init__(self, name: str, registry: Registry, llm: LLM, broker: Broker, slot: int | None,
                 max_steps: int = 4, user_name: str = ""):
        self.name = name
        self.registry = registry
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

    def propose(self, messages: list[dict], seed: int | None = None, temperature: float = 0.0,
                schema: dict | None = None) -> dict:
        """One constrained model step (no execution)."""
        c = self.llm.chat(messages, schema or self.schema, slot=self.slot, temperature=temperature,
                          max_tokens=800, seed=seed)
        return c.data or {}

    def first_messages(self, task: str, now: dt.datetime | None = None) -> list[dict]:
        now = now or dt.datetime.now()
        # the time goes in the user turn so the system prompt stays identical and cached
        return [{"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"[{now:%A %Y-%m-%d %H:%M}] {task}"}]

    # ------------------------------------------------------------------ run
    def run(self, task: str, command: str = "") -> AgentRun:
        out = AgentRun(self.name, task)
        messages = self.first_messages(task)
        evidence = guard.Evidence()
        evidence.add(command or task)
        evidence.add(task)
        try:
            for _ in range(self.max_steps):
                step = self.propose(messages)
                tool, args = step.get("tool", ""), step.get("args", {})
                if tool == "ask_user":
                    out.steps.append(Step(tool, args))
                    return self._ask(out, args.get("question", ""))
                if tool == "reply":
                    out.steps.append(Step(tool, args))
                    return self._finish(out, messages, args.get("text", ""), evidence)

                t = self.registry.get(tool)
                if t is not None:
                    # layer 2: entity check
                    missing = guard.missing_entities(t, args)
                    if missing:
                        out.steps.append(Step(tool, args, "blocked: missing " + ", ".join(f"{k} {v}" for k, v in missing)))
                        out.guard_events.append(f"L2 blocked {tool}: missing {missing}")
                        return self._ask(out, guard.entity_question(missing))
                    # layer 5: agreement on risky calls
                    if t.needs_confirmation(args):
                        samples = [step] + [self.propose(messages, seed=1000 + i, temperature=0.7)
                                            for i in range(AGREEMENT_SAMPLES - 1)]
                        chosen = guard.agree(samples)
                        out.guard_events.append(f"L5 samples {[guard.normalise_call(s) for s in samples]}")
                        if chosen is None or chosen.get("tool") in META:
                            out.steps.append(Step(tool, args, "blocked: samples disagree"))
                            opts = sorted({f"{s.get('tool')}({json.dumps(s.get('args'))})" for s in samples})
                            return self._ask(out, "I'm not sure exactly what you want me to do. I considered: "
                                             + "; ".join(opts) + ". Which one do you mean")
                        tool, args = chosen["tool"], chosen["args"]
                        t = self.registry.get(tool)
                        missing = guard.missing_entities(t, args) if t else []
                        if missing:
                            out.steps.append(Step(tool, args, "blocked: missing entity"))
                            return self._ask(out, guard.entity_question(missing))

                decision, result = self.broker.execute(self.name, tool, args)
                s = Step(tool, args, decision.reason, result.to_dict())
                if t is not None and result.ok and t.mutates(args):
                    # layer 6: read-back
                    s.mutating = True
                    if t.readback is None:
                        s.verified, s.readback = False, "its effect cannot be checked automatically"
                    else:
                        try:
                            s.verified, s.readback = t.readback(args, result.data)
                        except Exception as exc:
                            s.verified, s.readback = False, f"read-back failed: {exc}"
                    out.guard_events.append(f"L6 {tool} verified={s.verified}: {s.readback}")
                out.steps.append(s)
                evidence.add(result.data if result.ok else {"error": result.error})
                # layer 4: a lookup that fails twice means stop, never keep guessing arguments
                if not result.ok and sum(1 for c in out.tool_calls if c.tool == tool
                                         and not (c.result or {}).get("ok")) >= 2:
                    out.guard_events.append(f"L4 {tool} failed twice: stopping")
                    out.outcome = "reply"
                    out.text = "\n".join([f"{guard.NOT_SURE} {tool} failed twice, so I stopped."]
                                         + guard.status_lines(out.tool_calls))
                    return out
                messages.append({"role": "assistant", "content": json.dumps({"tool": tool, "args": args})})
                messages.append({"role": "user", "content": _result_message(tool, decision.reason, result, s)})
        except LLMError as exc:
            ok_calls = [s for s in out.tool_calls if (s.result or {}).get("ok")]
            if "truncated" in str(exc) and ok_calls:
                # the model's answer was cut off after the tools ran: give the tool output instead
                last = ok_calls[-1]
                out.outcome = "reply"
                out.guard_events.append("answer cut off; replying with the tool output")
                out.text = "\n".join([f"{guard.NOT_SURE[:-1]} how to put that briefly. Here is what {last.tool} "
                                      f"returned: {guard.compact(last.result.get('data'))}"]
                                     + guard.status_lines(out.tool_calls))
                return out
            out.outcome = "error"
            why = "the model's answer was cut off" if "truncated" in str(exc) else f"I couldn't reach the model ({exc})"
            out.text = "\n".join([f"{guard.COULDNT_VERIFY}: {why}."] + guard.status_lines(out.tool_calls))
            return out
        out.outcome = "step_limit"
        out.text = "\n".join([f"{guard.NOT_SURE} I stopped after {self.max_steps} steps without finishing."]
                             + guard.status_lines(out.tool_calls))
        return out

    def _ask(self, out: AgentRun, question: str) -> AgentRun:
        q = question.strip() or "Can you tell me more about what you want"
        if not q.endswith("?"):
            q = q.rstrip(".") + "?"
        lines = guard.status_lines(out.tool_calls)
        out.outcome, out.text = "ask", "\n".join(lines + [q])
        return out

    def _finish(self, out: AgentRun, messages: list[dict], text: str, ev: guard.Evidence) -> AgentRun:
        calls = out.tool_calls
        ok_calls = [s for s in calls if (s.result or {}).get("ok")]
        lines = guard.status_lines(calls)
        out.outcome = "reply"
        # layer 3: no successful tool result -> no facts
        if not ok_calls:
            if not calls:
                out.guard_events.append("L3 reply without any tool result replaced")
                out.text = f"{guard.NOT_SURE} I don't have a tool result that answers this."
            else:
                out.guard_events.append("L3/L4 reply after only failed calls replaced by status")
                out.text = "\n".join(lines)
            return out
        # layer 7: reply checker, one regeneration, then a code-built fallback
        problems = guard.check_reply(text, ev)
        if problems:
            out.guard_events.append(f"L7 problems: {problems}")
            fb = messages + [
                {"role": "assistant", "content": json.dumps({"tool": "reply", "args": {"text": text}})},
                {"role": "user", "content": "Your reply contains things that are not in the tool results: "
                 + "; ".join(problems) + ". Reply again using only values exactly as they appear in the "
                 "tool results, with the same units."}]
            try:
                text = self.propose(fb, schema=REPLY_ONLY).get("args", {}).get("text", "")
            except LLMError:
                text = ""
            problems = guard.check_reply(text, ev)
            if problems:
                out.guard_events.append(f"L7 still wrong after regeneration: {problems}; using tool output")
                last = ok_calls[-1]
                text = (f"{guard.NOT_SURE[:-1]} how to answer that exactly. Here is what {last.tool} returned: "
                        f"{guard.compact(last.result.get('data'))}")
        # layer 6: no "done" unless a state change was read back
        mutations = [s for s in calls if s.mutating]
        if guard.claims_done(text) and not any(s.verified for s in mutations):
            out.guard_events.append("L6 'done' claim without verified read-back removed")
            text = guard.NOT_SURE + " I can't confirm that anything changed."
        # layer 9: "the information is not there" is always said as "I'm not sure"
        normalised = guard.normalise_abstention(text)
        if normalised != text:
            out.guard_events.append("L9 unavailability reply normalised to 'I'm not sure'")
            text = normalised
        # layer 8: research answers carry sources; sentences no source supports are flagged
        if self.name == "research" and text and not text.lower().startswith("i'm not sure"):
            srcs = research_sources(ok_calls)
            if srcs is not None:
                checked = citations.check(text, srcs)
                out.guard_events.append(f"L8 {checked.label}: {len(checked.unsupported)} unsupported of "
                                        f"{len(checked.sentences)} sentences")
                text = checked.render()
        out.text = "\n".join([text] + lines) if text else "\n".join(lines)
        return out


TEXT_TOOLS = {"search_notes", "read_document", "summarize_document", "arxiv_search", "web_search", "fetch_page"}


def research_sources(ok_calls: list[Step]) -> list[citations.Source] | None:
    """Text sources behind a research answer, or None if no text tool was used (then layer 8 does
    not apply: e.g. dataset_info numbers are checked by layer 7)."""
    if not any(s.tool in TEXT_TOOLS for s in ok_calls):
        return None
    out: list[citations.Source] = []

    def add(ref, text):
        if ref and text:
            out.append(citations.Source(len(out) + 1, str(ref), str(text)))

    for s in ok_calls:
        d = (s.result or {}).get("data") or {}
        if s.tool == "search_notes":
            for part in d.get("searches", [d]):
                for h in part.get("results", []):
                    add(h.get("path"), h.get("snippet"))
        elif s.tool == "read_document":
            add(d.get("path"), d.get("text"))
        elif s.tool == "summarize_document":
            add(d.get("path"), " ".join(x["text"] for x in d.get("sentences", []) if x.get("supported")))
        elif s.tool == "arxiv_search":
            for p in d.get("papers", []):
                add(p.get("url"), f"{p.get('title')}. {p.get('summary')}")
        elif s.tool == "web_search":
            for r in d.get("results", []):
                add(r.get("url"), f"{r.get('title')}. {r.get('snippet')}")
        elif s.tool == "fetch_page":
            add(d.get("url"), d.get("text"))
    return out


def _result_message(tool: str, decision: str, result: ToolResult, step: Step) -> str:
    body = json.dumps(result.to_dict(), default=str)
    if len(body) > 6000:
        body = body[:6000] + "...(truncated)"
    rb = f"\nREAD-BACK: {'verified' if step.verified else 'NOT verified'}: {step.readback}" if step.mutating else ""
    return f"TOOL RESULT for {tool} ({decision}):\n{body}{rb}"
