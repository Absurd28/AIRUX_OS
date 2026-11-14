"""Assistant agent tools on the memory vault: reminders, calendar, agenda/briefing, memory.

Reminders and events are vault 'schedule' items, remembered facts are 'profile' items.
"what do you know about me" = recall with no topic; "forget X" = forget; "export my memory",
"pause memory" = memory_control.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from ..memory.vault import Vault, iso
from .base import Registry, Risk, S, Tool, ToolError, exists_path, expand
from .timeparse import parse_when


def make_tools(vault: Vault, key_source: str = "") -> list[Tool]:
    A = "assistant"

    def when(text: str) -> dt.datetime:
        try:
            return parse_when(text, vault.clock())
        except ValueError as exc:
            raise ToolError(f"could not understand the time: {exc}") from None

    def time_entity(*keys: str):
        """Layer 2: a time that cannot be parsed is a missing detail -> ask before calling."""
        def ent(a: dict) -> list:
            out = []
            for k in keys:
                if a.get(k):
                    try:
                        parse_when(a[k], vault.clock())
                        out.append(("time", a[k], True))
                    except ValueError:
                        out.append(("time", repr(a[k]), False))
            return out
        return ent

    def add_reminder(text: str, when_: str) -> dict:
        due = when(when_)
        if due < vault.clock():
            raise ToolError(f"that time is in the past: {due.isoformat(timespec='minutes')}")
        iid = vault.add("schedule", text, event_time=due, meta={"type": "reminder"})
        return {"id": iid, "text": text, "due": due.isoformat(timespec="minutes")}

    def add_event(title: str, start: str, end: str = "", location: str = "") -> dict:
        s = when(start)
        e = when(end) if end else None
        if e and e <= s:
            raise ToolError("the event ends before it starts")
        meta = {"type": "event", "end": iso(e) if e else None, "location": location or None, "source": "voice"}
        iid = vault.add("schedule", title, event_time=s, meta=meta)
        return {"id": iid, "title": title, "start": s.isoformat(timespec="minutes"),
                "end": e.isoformat(timespec="minutes") if e else None}

    def agenda(day: str = "today") -> dict:
        d = day.strip().lower()
        today = vault.clock().date()
        if d in ("today", ""):
            target = today
        elif d == "tomorrow":
            target = today + dt.timedelta(days=1)
        else:
            try:
                target = dt.date.fromisoformat(d)
            except ValueError:
                raise ToolError(f"use today, tomorrow or YYYY-MM-DD, not {day!r}") from None
        items = vault.day_schedule(target)
        out = {"day": target.isoformat(),
               "events": [{"title": i.text, "start": i.event_time, "end": i.meta.get("end"),
                           "location": i.meta.get("location")} for i in items if i.meta.get("type") == "event"],
               "reminders": [{"text": i.text, "due": i.event_time, "fired": bool(i.meta.get("fired"))}
                             for i in items if i.meta.get("type") == "reminder"]}
        if target == today:
            out["watched_jobs"] = [{"label": l, "status": s or "running", "finished": f} for l, s, f in
                                   vault.db.execute("SELECT label, status, finished FROM jobs ORDER BY id DESC "
                                                    "LIMIT 20").fetchall()]
        return out

    def remember(fact: str) -> dict:
        iid = vault.add("profile", fact, importance=2.0)
        return {"id": iid, "remembered": fact}

    def recall(query: str = "") -> dict:
        if not query.strip():  # "what do you know about me"
            prof = vault.by_kind("profile", order="importance DESC")
            vault.touch([p.id for p in prof])
            counts = {k: n for k, n in vault.db.execute("SELECT kind, count(*) FROM items GROUP BY kind")}
            return {"profile_facts": [p.text for p in prof], "items_by_kind": counts, "paused": vault.paused}
        hits = vault.search(query, k=8, kinds=("profile", "episode", "schedule"))
        return {"query": query, "count": len(hits),
                "results": [{"kind": h.kind, "text": h.text, "date": (h.event_time or h.created)[:16]} for h in hits]}

    def forget(topic: str) -> dict:
        removed = vault.forget(topic)
        return {"topic": topic, "items_removed": len(removed), "removed": [r.text for r in removed][:20]}

    def forget_readback(a: dict, result) -> tuple[bool, str]:
        left = vault.topic_present(a["topic"])
        ok = not left["keyword"] and not left["vector"]
        return ok, ("nothing about it remains in keyword or vector search" if ok
                    else f"still found: keyword {left['keyword']}, vector {left['vector']}")

    def import_calendar(path: str) -> dict:
        p = expand(path)
        if not p.is_file():
            raise ToolError(f"no such file: {p}")
        events = parse_ics(p.read_text(errors="replace"))
        if not events:
            raise ToolError(f"no events found in {p}")
        for ev in events:
            vault.add("schedule", ev["title"], event_time=dt.datetime.fromisoformat(ev["start"]),
                      meta={"type": "event", "end": ev.get("end"), "location": ev.get("location"), "source": str(p)})
        return {"file": str(p), "imported": len(events)}

    def import_readback(a: dict, result) -> tuple[bool, str]:
        n = sum(1 for i in vault.by_kind("schedule") if i.meta.get("source") == result["file"])
        return n >= result["imported"], f"{n} events from {result['file']} are stored"

    def memory_control(action: str) -> dict:
        if action == "pause":
            vault.set_paused(True)
            return {"paused": True, "note": "conversations are not recorded until you resume"}
        if action == "resume":
            vault.set_paused(False)
            return {"paused": False}
        if action == "status":
            counts = {k: n for k, n in vault.db.execute("SELECT kind, count(*) FROM items GROUP BY kind")}
            return {"paused": vault.paused, "items_by_kind": counts, "size_mb": round(vault.size_bytes() / 2**20, 2),
                    "quota_mb": vault.quota // 2**20, "encrypted": True, "key_source": key_source or "unknown"}
        if action == "export":
            dest = Path.home() / f"aira-memory-export-{vault.clock():%Y%m%d-%H%M%S}.json"
            return vault.export(dest)
        raise ToolError(f"unknown action {action}")

    def control_readback(a: dict, result) -> tuple[bool, str]:
        if a["action"] in ("pause", "resume"):
            ok = vault.paused == (a["action"] == "pause")
            return ok, f"memory is {'paused' if vault.paused else 'recording'}"
        if a["action"] == "export":
            p = Path(result["file"])
            try:
                n = len(json.loads(p.read_text())["items"])
            except (OSError, ValueError, KeyError) as exc:
                return False, f"export file unreadable: {exc}"
            return n == result["items"], f"{p} holds {n} items"
        return True, "read only"

    def row_readback(a: dict, result) -> tuple[bool, str]:
        it = vault.get(result["id"])
        return it is not None, f"item {result['id']} is stored in the vault" if it else "item not found in the vault"

    reminder = Tool("add_reminder", A, "set a reminder; when is plain words like 'in 2 minutes', '18:30', 'tomorrow 9am'",
                    {"text": S("what to remind about"), "when": S("when, in plain words")}, ["text", "when"],
                    lambda text, when: add_reminder(text, when), frozenset({Risk.WRITE}),
                    entities=time_entity("when"), readback=row_readback, changes_state=True)
    return [
        reminder,
        Tool("add_event", A, "add a calendar event; times in plain words like 'tomorrow 14:00'",
             {"title": S("event title"), "start": S("start time"), "end": S("end time"), "location": S("where")},
             ["title", "start"], add_event, frozenset({Risk.WRITE}), entities=time_entity("start", "end"),
             readback=row_readback, changes_state=True),
        Tool("agenda", A, "events, reminders and watched jobs for a day; also the daily briefing",
             {"day": S("today, tomorrow or YYYY-MM-DD")}, [], agenda),
        Tool("remember", A, "remember a fact about the user for later", {"fact": S("the fact")}, ["fact"],
             remember, frozenset({Risk.WRITE}), readback=row_readback, changes_state=True),
        Tool("recall", A, "what AIRA remembers about a topic; with no topic, everything it knows about the user",
             {"query": S("topic, or empty for everything about the user")}, [], recall),
        Tool("forget", A, "forget everything remembered about a topic", {"topic": S("topic to forget")},
             ["topic"], forget, frozenset({Risk.DELETE}), readback=forget_readback, changes_state=True,
             redact_log=True),
        Tool("import_calendar", A, "import events from an .ics calendar file", {"path": S(".ics file path")},
             ["path"], import_calendar, frozenset({Risk.WRITE}),
             entities=lambda a: [exists_path(a["path"], "calendar file")], readback=import_readback,
             changes_state=True),
        Tool("memory_control", A, "export the memory to a file, pause or resume recording, or show memory status",
             {"action": S("what to do", enum=["export", "pause", "resume", "status"])}, ["action"], memory_control,
             lambda a: {Risk.WRITE} if a.get("action") in ("export", "pause", "resume") else {Risk.READ},
             readback=control_readback, changes_state=lambda a: a.get("action") != "status"),
    ]


def _ics_time(v: str) -> str:
    v = v.strip()
    if "T" in v:
        base = dt.datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
        if v.endswith("Z"):
            base = base.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)
        return base.isoformat(timespec="minutes")
    return dt.datetime.strptime(v[:8], "%Y%m%d").isoformat(timespec="minutes")


def parse_ics(text: str) -> list[dict]:
    # unfold continuation lines (RFC 5545 3.1)
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    events, cur = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            if "title" in cur and "start" in cur:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key, val = line.split(":", 1)
            name = key.split(";")[0]
            if name == "SUMMARY":
                cur["title"] = val.replace("\\,", ",").replace("\\n", " ")
            elif name == "DTSTART":
                cur["start"] = _ics_time(val)
            elif name == "DTEND":
                cur["end"] = _ics_time(val)
            elif name == "LOCATION":
                cur["location"] = val.replace("\\,", ",")
    return events


def register(reg: Registry, vault: Vault, key_source: str = "") -> None:
    for t in make_tools(vault, key_source):
        reg.add(t)
