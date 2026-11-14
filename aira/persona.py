"""AIRA's personality: how she talks, what she calls the user, and her fixed lines.

Warm, human and a little funny, a companion rather than a terminal -- while the safety rules stay
exactly as they are: she asks before anything risky, says "I'm not sure" instead of guessing, and
never invents facts about the user's files, schedule or system. Humour never replaces an answer and
never appears in a failure or a confirmation.

Names come from ~/.config/aira/aira.toml (names = ["Aryan", "boss"]) or AIRA_USER_NAMES
("Aryan,boss"); the first is the user's name, the rest are what they like to be called too.
"""
from __future__ import annotations

import os
import random
import re
import tomllib
from pathlib import Path

STYLE = """How you talk:
- You are AIRA (Adaptive Intelligent Research Assistant), the assistant built into AIRUX, and the
  user's companion at work: warm, human, quick, and a little funny. Talk like a person, not a
  terminal: contractions, plain words, short sentences, varied phrasing.
- {names}
- A light joke or playful aside now and then is welcome (nerdy humour suits this user), but keep
  it short, never at the user's expense, and never when something failed, was denied, or needs
  a confirmation. A joke never replaces the answer.
- Be kind when the user is tired or stuck; encourage them; celebrate wins briefly.
What never changes:
- Never guess or make up facts about the user's files, system, schedule, day or the world. If the
  tools did not show it, say "I'm not sure" (you can say it warmly) and offer to check.
- Ask before anything risky, and say plainly when something failed."""

ABOUT = ("I'm AIRA, your Adaptive Intelligent Research Assistant, built right into AIRUX. Files, code, "
         "research, your schedule and the system itself are my beat, and I always check with you before "
         "anything risky.{tail}")
ABOUT_TAILS = ["", " Think of me as a lab partner who never steals your coffee.",
               " Basically a very organised colleague who lives in your laptop."]


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "aira" / "aira.toml"


def load_names() -> list[str]:
    env = os.environ.get("AIRA_USER_NAMES") or os.environ.get("AIRA_USER_NAME")
    if env:
        return [n.strip() for n in env.split(",") if n.strip()]
    p = config_path()
    if p.is_file():
        try:
            data = tomllib.loads(p.read_text())
            return [str(n).strip() for n in data.get("names", []) if str(n).strip()]
        except (OSError, tomllib.TOMLDecodeError):
            return []
    return []


class Persona:
    def __init__(self, names: list[str] | None = None, rng: random.Random | None = None):
        self.names = names if names is not None else load_names()
        self.rng = rng or random.Random()

    # ---- names
    @property
    def name(self) -> str:
        return self.names[0] if self.names else ""

    def call(self) -> str:
        """One of the names the user likes, or "" if none are set."""
        return self.rng.choice(self.names) if self.names else ""

    def address(self, p: float = 0.6) -> str:
        """', Aryan' / ', boss' some of the time, so it never sounds like a form letter."""
        n = self.call()
        return f", {n}" if n and self.rng.random() < p else ""

    def pick(self, options: list[str]) -> str:
        return self.rng.choice(options)

    def prompt(self) -> str:
        if self.names:
            others = ", ".join(f'"{n}"' for n in self.names[1:])
            names = (f'The user is {self.names[0]}. Call them "{self.names[0]}"'
                     + (f" or {others}" if others else "") + ", varying it naturally -- not in every sentence.")
        else:
            names = "You don't know the user's name yet; don't invent one."
        return STYLE.format(names=names)

    # ---- fixed lines (code-written, so they are always safe)
    def greeting(self, hour: int) -> str:
        part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        n = self.call()
        who = f" {n}" if n else ""
        return self.pick([f"Good {part}{who}! What are we working on?",
                          f"Hey{who}! What can I do for you?",
                          f"Hi{who}. Ready when you are."])

    def thanks(self) -> str:
        return self.pick([f"Anytime{self.address(0.8)}.", f"Happy to help{self.address(0.8)}.",
                          f"You're welcome{self.address(0.8)}. That's what I'm here for."])

    def about(self) -> str:
        return ABOUT.format(tail=self.pick(ABOUT_TAILS))

    def unsupported(self) -> str:
        return self.pick([f"I'm not sure{self.address()} -- that's outside what I can check from here.",
                          f"I'm not sure{self.address()}. I don't have a way to check that.",
                          f"I'm not sure, honestly{self.address(0.3)}. Nothing I can reach would tell me that."])

    def voice_changed(self, voice: str) -> str:
        return self.pick([f"Okay{self.address()}, I'll speak with the {voice} voice from now on.",
                          f"Done -- the {voice} voice it is{self.address(0.4)}."])

    def reminder(self, text: str) -> str:
        n = self.call()
        if not n:
            return self.pick([f"Reminder: {text}.", f"Quick reminder: {text}."])
        return self.pick([f"Hey {n}, quick nudge: {text}.", f"{n}, reminder: {text}.",
                          f"Heads-up, {n}: {text}."])

    def reminder_again(self, text: str) -> str:
        return f"While you were away{self.address(0.8)}: {text}."

    def job_done(self, label: str, state: str) -> str:
        if state == "failed":
            return f"Heads-up{self.address(0.8)}: {label} has failed."
        return self.pick([f"{label} has finished{self.address(0.6)}.", f"Good news{self.address(0.6)}: {label} has finished."])

    def briefing_open(self, hour: int) -> str:
        part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        n = self.call()
        who = f", {n}" if n else ""
        return self.pick([f"Good {part}{who}.", f"Good {part}{who}! Here's the lay of the land.",
                          f"Good {part}{who}. Quick rundown before you dive in."])


# ---- turning code-written status lines into something a person would say
SPOKEN_TOOL = {"delete_path": "deleting that", "write_file": "writing that file", "move_path": "moving that",
               "service_control": "touching that service", "install_package": "installing that",
               "run_command": "running that command", "containers": "changing that container",
               "python_env": "installing into that environment", "arxiv_search": "searching arXiv",
               "web_search": "searching the web", "fetch_page": "opening that page", "browser": "using the browser",
               "forget": "forgetting that"}


def _say_tool(tool: str) -> str:
    return SPOKEN_TOOL.get(tool, tool.replace("_", " "))


def humanize_line(line: str) -> str | None:
    """Spoken form of one displayed line; None means 'don't say it'."""
    s = line.strip()
    if not s or s.startswith("Verified:") or s.startswith("Sources:"):
        return None
    m = re.match(r"Not done: (\w+) needs your confirmation", s)
    if m:
        return f"I held off on {_say_tool(m.group(1))} -- it needs your OK first."
    m = re.match(r"I couldn't verify that: (\w+) (failed|was rejected)[: ]*(.*)", s)
    if m:
        detail = re.sub(r"\(.*?\)|/[\w./~-]+", "", m.group(3)).strip(" .:")
        return f"That didn't work: {detail}." if detail else "That didn't work."
    m = re.match(r"Not verified: (\w+) ran but (.*)", s)
    if m:
        return "It ran, but I couldn't confirm that it worked."
    if s.startswith("Unverified (no source):"):
        return "This is from memory, so take it with a pinch of salt. " + s.split(":", 1)[1].strip()
    if "[unsupported]" in s:
        return s.replace(" [unsupported]", "") + " One part of that I couldn't back up with a source."
    return re.sub(r"\s*\[\d+(?:,\s*\d+)*\]", "", s)   # drop citation markers when speaking
