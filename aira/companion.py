"""Companion chat: small talk, jokes, encouragement -- no tools, no facts.

Used when the router says the user is just talking ("how are you", "tell me a joke", "I'm wiped").
The model may use what the vault's profile says about the user, and nothing else about them. The
reply goes through the reply checker against that small evidence (numbers, paths, file names it
did not get are not allowed), is regenerated once if needed, and otherwise falls back to a fixed
friendly line. Factual questions never come here: the router sends those to an agent.
"""
from __future__ import annotations

import datetime as dt

from . import guard
from .llm import LLM, LLMError
from .persona import Persona

RULES = """You are just chatting with the user right now: no tools, no tasks.
- Reply in 1 to 3 short sentences, like a friend at the next desk.
- You may use only these facts about the user (from your memory): {facts}
- Do not state anything about their files, computer, schedule, day, health or the outside world as
  fact, and do not assume what they did, are doing or will do (no "hope you had a good weekend",
  no "your meeting went well") or feel beyond what they just told you. If they ask something
  factual, say you'd need to check and offer to.
- No emoji (your words are spoken aloud).
- If asked who you are, say you're AIRA, the Adaptive Intelligent Research Assistant.
- Jokes: short, kind, nerdy is good. If they sound stressed or tired, be supportive first."""
SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
          "additionalProperties": False}


class Companion:
    def __init__(self, llm: LLM, persona: Persona, vault=None, slot: int | None = 0):
        self.llm, self.persona, self.vault, self.slot = llm, persona, vault, slot

    def facts(self) -> list[str]:
        if self.vault is None:
            return []
        return [p["text"] for p in self.vault.hot.get("profile", [])][:20]

    def reply(self, command: str, now: dt.datetime | None = None) -> tuple[str, list[str]]:
        """Returns (reply, guard events)."""
        now = now or dt.datetime.now()
        facts = self.facts()
        system = (self.persona.prompt() + "\n\n"
                  + RULES.format(facts="; ".join(facts) if facts else "(nothing yet)"))
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": f"[{now:%A %H:%M}] {command}"}]
        ev = guard.Evidence()
        ev.add(command)
        for f in facts:
            ev.add(f)
        events = []
        for attempt in range(2):
            try:
                text = (self.llm.chat(msgs, SCHEMA, slot=self.slot, temperature=0.8, max_tokens=200).data
                        or {}).get("text", "").strip()
            except LLMError as exc:
                events.append(f"companion: model unavailable ({exc})")
                break
            problems = guard.check_reply(text, ev)
            if text and not problems:
                return text, events
            events.append(f"companion L7 problems: {problems or ['empty']}")
            msgs = msgs + [{"role": "assistant", "content": text},
                           {"role": "user", "content": "Say that again without any numbers, names, paths or "
                            "facts you were not given."}]
        events.append("companion: using a fixed line")
        return self.persona.pick([f"I'm here{self.persona.address(0.8)}. What's on your mind?",
                                  f"Always happy to chat{self.persona.address(0.8)}."]), events
