"""Grounding guard (hallucination control), enforced in code.

Layer 1  constrained output: JSON-schema grammar (agents.py) + broker validation (broker.py)
Layer 2  entity check: everything a call refers to must exist, else AIRA asks (entity_question)
Layer 3  facts only from tools: a reply with no successful tool result behind it is replaced
Layer 4  failed lookup means stop: failures and denials become plain code-written status lines
Layer 5  agreement: risky calls are sampled 3 times; act only if 2 match (agree)
Layer 6  read-back: state-changing calls are verified before anything is called "done"
Layer 7  reply checker: numbers, paths and names in a reply must appear in the evidence
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .tools.base import Tool, ToolError, expand

NOT_SURE = "I'm not sure."
COULDNT_VERIFY = "I couldn't verify that"

# ---------------- layer 2 ----------------


def missing_entities(tool: Tool, args: dict) -> list[tuple[str, str]]:
    if tool.entities is None:
        return []
    try:
        triples = tool.entities(args)
    except Exception as exc:  # cannot check -> treat as missing, never act blind
        return [("item", f"{args} ({exc})")]
    return [(kind, value) for kind, value, ok in triples if not ok]


def _suggest_path(value: str) -> str | None:
    try:
        p = expand(value)
    except ToolError:
        # ~name -> suggest ~/name if that exists
        alt = "~/" + value.lstrip("~")
        try:
            return alt if expand(alt).exists() else None
        except ToolError:
            return None
    parent = p.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    try:
        names = [c.name for c in parent.iterdir()]
    except OSError:
        return None
    close = difflib.get_close_matches(p.name, names, n=1, cutoff=0.6)
    if not close:
        return None
    home = str(Path.home())
    s = str(parent / close[0])
    return "~" + s[len(home):] if s.startswith(home) else s


def entity_question(missing: list[tuple[str, str]]) -> str:
    kind, value = missing[0]
    if kind == "time":
        return f"I couldn't understand the time {value}. When exactly should it be?"
    q = f"I couldn't find the {kind} {value}."
    if kind in ("path", "file", "folder", "document", "dataset", "calendar file", "project folder"):
        hint = _suggest_path(value)
        if hint:
            return f"{q} Did you mean {hint}?"
    return f"{q} Can you tell me exactly which {kind} you mean?"


# ---------------- layer 5 ----------------


def normalise_call(call: dict) -> str:
    args = {}
    for k, v in sorted((call.get("args") or {}).items()):
        if isinstance(v, str):
            v = " ".join(v.split())
            if v.startswith(("~", "/")):
                try:
                    v = str(expand(v))
                except ToolError:
                    pass
        args[k] = v
    return json.dumps({"tool": call.get("tool"), "args": args}, sort_keys=True)


def agree(samples: list[dict], need: int = 2) -> dict | None:
    """Return the call proposed by at least `need` samples, else None."""
    counts: dict[str, list[dict]] = {}
    for s in samples:
        counts.setdefault(normalise_call(s), []).append(s)
    best = max(counts.values(), key=len)
    return best[0] if len(best) >= need else None


# ---------------- layer 7 ----------------

NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")
PATH_RE = re.compile(r"(?:~|/)[\w.\-/~]*[\w]")
FILE_RE = re.compile(r"\b[\w\-]+\.(?:[a-z0-9]{1,5})\b", re.I)
QUOTED_RE = re.compile(r"`([^`]{2,80})`")
UNIT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(GB|MB|TB)\b")
ABBREVIATIONS = {"e.g", "i.e", "a.m", "p.m", "etc.", "vs."}


NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
                "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90, "hundred": 100}


@dataclass
class Evidence:
    texts: list[str] = field(default_factory=list)
    gib_values: set[float] = field(default_factory=set)
    list_lengths: set[int] = field(default_factory=set)

    def add(self, obj) -> None:
        self.texts.append(obj if isinstance(obj, str) else json.dumps(obj, default=str))
        self._walk(obj)

    def _walk(self, obj, key: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                self._walk(v, k)
        elif isinstance(obj, list):
            self.list_lengths.add(len(obj))
            for v in obj:
                self._walk(v, key)
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool) and "gib" in key.lower():
            self.gib_values.add(float(obj))

    @property
    def blob(self) -> str:
        return "\n".join(self.texts)

    def numbers(self) -> list[float]:
        blob = self.blob.replace("\\n", " ")
        # "2026-11-14T17:46" holds an hour and a minute; a reply may say 17:46 or 5:46 pm
        blob = re.sub(r"(\d)T(\d)", r"\1 \2", blob)
        out = [float(x) for x in NUM_RE.findall(blob)]
        for h, _m in re.findall(r"\b(\d{1,2}):(\d{2})\b", blob):
            out.append(float(int(h) % 12 or 12))
        # numbers the user said as words ("in two minutes") support the same digits in the reply
        out += [float(NUMBER_WORDS[w]) for w in re.findall(r"[a-z]+", blob.lower()) if w in NUMBER_WORDS]
        return out + [float(n) for n in self.list_lengths]


UNIT_AFTER = re.compile(r"\s*(%|[KMGT]i?B\b|°|degrees|tok|ms\b|s\b|seconds?|minutes?|hours?|W\b)", re.I)


def _num_supported(value: str, evidence_nums: list[float], measured: bool) -> bool:
    """Exact match always counts. A rounded match counts only for measurements (number + unit),
    so "4 GiB" may round 4.36, but "4 rows" must match a 4 exactly."""
    v = float(value)
    decimals = len(value.split(".")[1]) if "." in value else 0
    for e in evidence_nums:
        if abs(e - v) < 1e-9:
            return True
        if measured and round(e, decimals) == v:
            return True
    return False


def _expanded(s: str) -> set[str]:
    out = {s}
    try:
        out.add(str(expand(s)))
    except ToolError:
        pass
    home = str(Path.home())
    if s.startswith(home):
        out.add("~" + s[len(home):])
    return out


def check_reply(reply: str, ev: Evidence) -> list[str]:
    """Return problems: claims in the reply that the evidence does not support."""
    problems = []
    blob = ev.blob
    nums = ev.numbers()
    # skip numbers that are part of paths/file names (checked as paths)
    reply_wo_paths = PATH_RE.sub(" ", FILE_RE.sub(" ", reply))
    for m in NUM_RE.finditer(reply_wo_paths):
        n = m.group(0)
        measured = bool(UNIT_AFTER.match(reply_wo_paths, m.end()))
        if not _num_supported(n, nums, measured):
            problems.append(f"number {n} is not in the tool results")
    for p in PATH_RE.findall(reply):
        if len(p) < 3:
            continue
        if not any(x in blob for x in _expanded(p.rstrip("/."))):
            problems.append(f"path {p} is not in the tool results")
    for f in FILE_RE.findall(reply):
        if re.fullmatch(r"\d+\.\d+[A-Za-z%°]*", f):  # a decimal number (maybe with a unit), not a file name
            continue
        if f.lower() in ABBREVIATIONS:
            continue
        if f.lower() not in blob.lower():
            problems.append(f"name {f} is not in the tool results")
    for q in QUOTED_RE.findall(reply):
        if q not in blob:
            problems.append(f"`{q}` is not in the tool results")
    for value, unit in UNIT_RE.findall(reply):
        if unit == "GB" and float(value) in ev.gib_values:
            problems.append(f"{value} is in GiB, not GB")
    problems += _day_word_problems(reply, blob)
    return problems


ISO_DATE = re.compile(r"\b(20\d\d-\d\d-\d\d)(?=[T\s\"']|$)")


def _day_word_problems(reply: str, blob: str, today: dt.date | None = None) -> list[str]:
    """"today" / "tomorrow" in the reply must match the dates in the tool results (when there are any)."""
    dates = {dt.date.fromisoformat(d) for d in ISO_DATE.findall(blob)}
    if not dates:
        return []
    today = today or dt.date.today()
    said = {w.lower() for w in re.findall(r"\b(today|tonight|tomorrow)\b", reply, re.I)}
    out = []
    if "tomorrow" in said and today + dt.timedelta(days=1) not in dates:
        out.append(f"says tomorrow, but the dates in the tool results are {sorted(str(d) for d in dates)}")
    if said & {"today", "tonight"} and today not in dates:
        out.append(f"says today, but the dates in the tool results are {sorted(str(d) for d in dates)}")
    return out


# ---------------- layer 3: topics no tool can see ----------------
# AIRA has no tool that reads email, chat apps, the weather, markets, bank accounts or contacts.
# A question about them can only be answered by guessing, so it is answered "I'm not sure" in code
# before any model sees it -- unless the user asks to search the web or use the browser, which can
# legitimately look such things up.
UNCOVERED = re.compile(
    r"\b(e-?mail(?:s|ed|ing)?|inbox|mailbox|text(?:ed)? messages?|texted|sms|whatsapp|telegram|"
    r"slack messages?|dms?|weather|forecast|temperature outside|stock prices?|share prices?|bitcoin|"
    r"crypto(?:currency)?|exchange rates?|bank balance|bank account|credit card|phone numbers?|"
    r"contact details|who called|missed calls?)\b", re.I)
WEB_INTENT = re.compile(r"\b(search|look up|google|browser|web|online|internet|arxiv|website|page)\b", re.I)
# commands that only mention such a topic while asking for something AIRA can do ("remind me to
# email Rao", "remember that my bank is X") are not questions about it
TASK_INTENT = re.compile(r"^\s*(please\s+)?(remind|remember|note|write|draft|add|create|schedule|set|"
                         r"make|save|forget)\b", re.I)


def uncovered_topic(command: str) -> str | None:
    """The topic word if the command asks about something no AIRA tool can see, else None."""
    m = UNCOVERED.search(command)
    if m and not WEB_INTENT.search(command) and not TASK_INTENT.search(command):
        return m.group(1).lower()
    return None


def topic_phrase(topic: str) -> tuple[str, bool]:
    """How to say what AIRA cannot see, and whether a web search could help (public information)."""
    t = topic.lower()
    for pattern, phrase, public in (
            (r"mail|inbox", "your email", False), (r"text|sms|whatsapp|telegram|slack|dm", "your messages", False),
            (r"call", "your calls", False), (r"phone|contact", "your contacts", False),
            (r"bank|credit", "your bank accounts", False), (r"weather|forecast|outside", "the weather", True),
            (r"bitcoin|crypto|stock|share|exchange", "live prices", True)):
        if re.search(pattern, t):
            return phrase, public
    return t, False


# ---------------- layers 3, 4, 6: code-written status ----------------

DONE_WORDS = re.compile(r"\b(done|deleted|removed|created|renamed|moved|installed|restarted|started|stopped|"
                        r"committed|saved|written|copied|set|scheduled|added|remembered|imported|forgot|"
                        r"forgotten|launched|opened)\b", re.I)
# any "...n't" word (don't, isn't, couldn't, haven't, ...) or a plain negation word
NEGATION = re.compile(r"(\w+n['’]t\b|\b(?:not|cannot|unable|failed|no|without|never|nothing)\b)", re.I)


def claims_done(text: str) -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if sentence.rstrip().endswith("?"):   # an offer or a question ("Want me to set it up?") claims nothing
            continue
        if DONE_WORDS.search(sentence) and not NEGATION.search(sentence):
            return True
    return False


# Layer 9 wording: a reply saying the information is not there is an abstention, and AIRA always
# says so with the same phrase. Only phrases about missing *information* count; "there are no
# uncommitted changes" is a grounded answer, not an abstention.
_ADV = r"(?:\w+ly )?"  # one optional adverb: "not directly reported", "doesn't currently show"
UNAVAILABLE = re.compile(
    rf"\b((?:not|isn't|aren't) {_ADV}(?:available|shown|provided|included|reported|listed|exposed)|"
    rf"is unavailable|no information|no data (?:on|about|for)|"
    rf"(?:doesn't|does not|don't|do not|didn't|did not) {_ADV}(?:show|include|report|provide|contain|list|"
    rf"mention|say|expose)|not (?:in|part of) the (?:tool )?(?:results?|output|data)|"
    rf"cannot be determined|can't be determined|couldn't find|could not find|no record of|"
    # the persona's chattier ways of saying "I don't have that"
    rf"(?:haven't|have not|hasn't|has not) {_ADV}(?:stored|saved|recorded|got|found|seen|noted)\b[^.?!]*\bany|"
    rf"(?:don't|do not) {_ADV}have any|nothing (?:stored|saved|recorded|noted)|"
    rf"no (?:facts|notes|records|events|reminders|memories|entries) (?:about|on|for|stored|saved))\b", re.I)


def normalise_abstention(text: str) -> str:
    """Prefix "I'm not sure." to a reply that says the information is unavailable."""
    t = text.strip()
    if not t or t.lower().startswith("i'm not sure") or not UNAVAILABLE.search(t):
        return text
    return f"{NOT_SURE} {t}"


def status_lines(steps) -> list[str]:
    """Plain, code-written outcome of every non-trivial call in a run."""
    lines = []
    for s in steps:
        r = s.result or {}
        if s.decision.startswith("blocked"):
            continue  # stopped by the guard before running; the question explains it
        if s.decision.startswith("denied"):
            lines.append(f"Not done: {s.tool} needs your confirmation and it was not given.")
        elif s.decision.startswith("rejected"):
            lines.append(f"{COULDNT_VERIFY}: {s.tool} was rejected ({s.decision[10:]}).")
        elif not r.get("ok"):
            lines.append(f"{COULDNT_VERIFY}: {s.tool} failed: {r.get('error', 'unknown error')}")
        elif s.mutating:
            if s.verified:
                lines.append(f"Verified: {s.readback}.")
            else:
                lines.append(f"Not verified: {s.tool} ran but {s.readback or 'its effect could not be checked'}.")
    return lines


UNIT_SUFFIXES = (("_gib", " GiB"), ("_mib", " MiB"), ("_percent", "%"), ("_c", " °C"), ("_w", " W"),
                 ("_seconds", " s"))


def _speakable(key: str, value) -> str:
    """"free_gib", 2.82 -> "free 2.82 GiB"; "due", "2026-11-14T18:52" -> "due 2026-11-14 18:52"."""
    if isinstance(value, str):
        value = re.sub(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", r"\1 \2", value)
    for suffix, unit in UNIT_SUFFIXES:
        if key.endswith(suffix) and isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{key[: -len(suffix)].replace('_', ' ')} {value}{unit}"
    return f"{key.replace('_', ' ')}: {value}"


def compact(obj, limit: int = 500) -> str:
    """Short human rendering of a tool result for fallback replies."""
    if isinstance(obj, dict):
        parts = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, default=str)
                v = v if len(v) < 160 else v[:160] + "..."
            parts.append(_speakable(str(k), v))
        s = "; ".join(parts)
    else:
        s = str(obj)
    return s if len(s) <= limit else s[:limit] + "..."
