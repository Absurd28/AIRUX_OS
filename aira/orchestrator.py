"""Orchestrator: turns one command into tasks for sub-agents and runs them in parallel."""
from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .agents import PERSONA, ROLES, AgentRun, SubAgent
from .broker import Broker, Confirmer
from .config import Config
from .llm import LLM, LLMError
from .memory.embed import Embedder
from .memory.keys import get_key
from .memory.vault import Vault
from .tools import build_registry
from .tools.base import AGENTS, Registry

ROUTER_RULES = """You are AIRA's orchestrator. Split the user's command into tasks for these agents:
{agents}

Answer with one JSON object, one of:
- {{"route": [{{"agent": <agent>, "task": <instruction for that agent>}}, ...]}}: one task per agent needed.
  Keep each task self-contained and keep the user's exact names, paths and times.
- {{"chat": "greeting" | "thanks" | "about"}}: a greeting, thanks, or a question about who AIRA is.
- {{"ask": <question>}}: if the command is too unclear to route.
- {{"unsupported": true}}: if no agent above has a way to find this out (for example the weather,
  prices, other people's private information, or things only the user knows).
Never answer questions yourself: route them, ask, or say unsupported."""

CHAT = {
    "greeting": "Hello{name}. I'm AIRA, your Adaptive Intelligent Research Assistant. What can I do for you?",
    "thanks": "You're welcome{name}.",
    "about": ("I'm AIRA, the Adaptive Intelligent Research Assistant built into AIRUX. I can check and control "
              "the system, your files, code, research material and your schedule, and I ask before anything risky."),
}
UNSUPPORTED = "I'm not sure. I don't have a tool that can check that."


@dataclass
class Turn:
    command: str
    route: list[dict] = field(default_factory=list)
    runs: list[AgentRun] = field(default_factory=list)
    reply: str = ""
    kind: str = ""  # "routed", "reply", "ask", "error"


class Orchestrator:
    def __init__(self, config: Config, confirm: Confirmer, registry: Registry | None = None,
                 llm: LLM | None = None, vault: Vault | None = None):
        config.ensure_dirs()
        self.config = config
        self.llm = llm or LLM(config.server_url, config.request_timeout)
        key_source = ""
        if vault is None:
            key, key_source = get_key()
            vault = Vault(config.vault_path, key, embedder=Embedder())
        self.vault = vault
        self.registry = registry or build_registry(config, vault, key_source)
        self.broker = Broker(self.registry, None, confirm,
                             sink=lambda e: vault.add("action", json.dumps(e, default=str), embed=False))
        try:
            slots = self.llm.total_slots()
        except LLMError:
            slots = 1
        self.slots = max(1, slots)
        # slot 0 = orchestrator, 1..5 = sub-agents (wrapping if the server has fewer slots)
        self.agents = {name: SubAgent(name, self.registry, self.llm, self.broker,
                                      slot=(i + 1) % self.slots, max_steps=config.max_agent_steps,
                                      user_name=config.user_name)
                       for i, name in enumerate(AGENTS)}
        self.router_prompt = PERSONA + "\n" + ROUTER_RULES.format(
            agents="\n".join(f"- {a}: {ROLES[a].split(': ', 1)[1]}" for a in AGENTS))
        self.router_schema = {"oneOf": [
            {"type": "object", "properties": {"route": {"type": "array", "minItems": 1, "maxItems": 5, "items": {
                "type": "object", "properties": {"agent": {"enum": list(AGENTS)}, "task": {"type": "string"}},
                "required": ["agent", "task"], "additionalProperties": False}}},
             "required": ["route"], "additionalProperties": False},
            {"type": "object", "properties": {"chat": {"enum": list(CHAT)}}, "required": ["chat"],
             "additionalProperties": False},
            {"type": "object", "properties": {"ask": {"type": "string"}}, "required": ["ask"],
             "additionalProperties": False},
            {"type": "object", "properties": {"unsupported": {"const": True}}, "required": ["unsupported"],
             "additionalProperties": False},
        ]}

    def action_log(self) -> list[dict]:
        """The action log entries from the vault, oldest first."""
        return [json.loads(it.text) for it in self.vault.by_kind("action", order="id")]

    def route(self, command: str) -> dict:
        now = dt.datetime.now()
        msgs = [{"role": "system", "content": self.router_prompt},
                {"role": "user", "content": f"[{now:%A %Y-%m-%d %H:%M}] {command}"}]
        return self.llm.chat(msgs, self.router_schema, slot=0, max_tokens=300).data or {}

    def handle(self, command: str) -> Turn:
        turn = self._handle(command)
        # episode memory (skipped while memory is paused)
        if not self.vault.paused and turn.kind != "error":
            first = turn.reply.strip().split("\n", 1)[0]
            forgot = any(s.tool == "forget" for r in turn.runs for s in r.tool_calls)
            # a "forget X" request must not put X straight back into memory
            text = "Forgot a topic on request." if forgot else f"{command} -> {first}"
            try:
                self.vault.add("episode", text)
            except Exception:
                pass
        return turn

    def _handle(self, command: str) -> Turn:
        turn = Turn(command)
        try:
            r = self.route(command)
        except LLMError as exc:
            turn.kind, turn.reply = "error", f"I couldn't reach the model: {exc}"
            return turn
        # the router never writes free-text answers (grounding layer 3): only fixed texts
        name = f", {self.config.user_name}" if self.config.user_name else ""
        if "chat" in r:
            turn.kind, turn.reply = "reply", CHAT[r["chat"]].format(name=name)
            return turn
        if "unsupported" in r:
            turn.kind, turn.reply = "unsupported", UNSUPPORTED
            return turn
        if "ask" in r:
            q = r["ask"].strip()
            turn.kind, turn.reply = "ask", q if q.endswith("?") else q.rstrip(".") + "?"
            return turn
        # merge duplicate agents into one task each
        tasks: dict[str, str] = {}
        for t in r.get("route", []):
            tasks[t["agent"]] = (tasks[t["agent"]] + " Also: " + t["task"]) if t["agent"] in tasks else t["task"]
        turn.route = [{"agent": a, "task": t} for a, t in tasks.items()]
        with ThreadPoolExecutor(max_workers=min(len(tasks), self.slots) or 1) as pool:
            turn.runs = list(pool.map(lambda kv: self.agents[kv[0]].run(kv[1], command), tasks.items()))
        turn.kind = "ask" if any(run.outcome == "ask" for run in turn.runs) else "routed"
        turn.reply = "\n".join(run.text for run in turn.runs if run.text)
        return turn


def turn_to_dict(turn: Turn) -> dict:
    return json.loads(json.dumps(turn, default=lambda o: o.__dict__))
