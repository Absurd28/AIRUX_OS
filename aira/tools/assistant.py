"""Assistant agent tools: reminders, calendar, remembered facts.

Phase 2 store: plain SQLite at Config.assistant_db. Phase 4 replaces it with the
SQLCipher memory vault (retention, decay, quota, vector search) behind the same tools.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
import threading
from pathlib import Path

from .base import I, Registry, Risk, S, Tool, ToolError, exists_path, expand
from .timeparse import parse_when

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, text TEXT NOT NULL, due TEXT NOT NULL,
  created TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, title TEXT NOT NULL, start TEXT NOT NULL,
  end TEXT, location TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, text TEXT NOT NULL, created TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path, clock=dt.datetime.now):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def q(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock, self._conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def x(self, sql: str, params: tuple = ()) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount


def _when(text: str, store: Store) -> dt.datetime:
    try:
        return parse_when(text, store.clock())
    except ValueError as exc:
        raise ToolError(f"could not understand the time: {exc}") from None


def make_tools(store: Store) -> list[Tool]:
    A = "assistant"

    def add_reminder(text: str, when: str) -> dict:
        due = _when(when, store)
        if due < store.clock():
            raise ToolError(f"that time is in the past: {due.isoformat(timespec='minutes')}")
        rid = store.x("INSERT INTO reminders(text, due, created) VALUES (?,?,?)",
                      (text, due.isoformat(timespec="minutes"), store.clock().isoformat(timespec="seconds")))
        return {"id": rid, "text": text, "due": due.isoformat(timespec="minutes")}

    def list_reminders(include_done: bool = False) -> dict:
        rows = store.q("SELECT id, text, due, done FROM reminders" + ("" if include_done else " WHERE done=0")
                       + " ORDER BY due")
        return {"count": len(rows), "reminders": rows}

    def add_event(title: str, start: str, end: str = "", location: str = "") -> dict:
        s = _when(start, store)
        e = _when(end, store) if end else None
        if e and e <= s:
            raise ToolError("the event ends before it starts")
        eid = store.x("INSERT INTO events(title, start, end, location, source) VALUES (?,?,?,?,?)",
                      (title, s.isoformat(timespec="minutes"), e.isoformat(timespec="minutes") if e else None,
                       location or None, "voice"))
        return {"id": eid, "title": title, "start": s.isoformat(timespec="minutes"),
                "end": e.isoformat(timespec="minutes") if e else None}

    def list_events(day: str = "today") -> dict:
        d = day.strip().lower()
        today = store.clock().date()
        if d in ("today", ""):
            target = today
        elif d == "tomorrow":
            target = today + dt.timedelta(days=1)
        else:
            try:
                target = dt.date.fromisoformat(d)
            except ValueError:
                raise ToolError(f"use today, tomorrow or YYYY-MM-DD, not {day!r}") from None
        rows = store.q("SELECT id, title, start, end, location FROM events WHERE substr(start,1,10)=? "
                       "ORDER BY start", (target.isoformat(),))
        return {"day": target.isoformat(), "count": len(rows), "events": rows}

    def remember(fact: str) -> dict:
        fid = store.x("INSERT INTO facts(text, created) VALUES (?,?)",
                      (fact, store.clock().isoformat(timespec="seconds")))
        return {"id": fid, "remembered": fact}

    def recall(query: str = "") -> dict:
        terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
        rows = store.q("SELECT id, text, created FROM facts ORDER BY id")
        if terms:
            rows = [r for r in rows if any(t in r["text"].lower() for t in terms)]
        return {"query": query, "count": len(rows), "facts": rows}

    def forget(topic: str) -> dict:
        terms = [t for t in re.findall(r"\w+", topic.lower()) if len(t) > 2]
        if not terms:
            raise ToolError("say what to forget")
        removed = 0
        for table, col in (("facts", "text"), ("reminders", "text"), ("events", "title")):
            for r in store.q(f"SELECT id, {col} AS t FROM {table}"):
                if all(t in r["t"].lower() for t in terms):
                    removed += store.x(f"DELETE FROM {table} WHERE id=?", (r["id"],))
        return {"topic": topic, "items_removed": removed}

    def import_calendar(path: str) -> dict:
        p = expand(path)
        if not p.is_file():
            raise ToolError(f"no such file: {p}")
        events = parse_ics(p.read_text(errors="replace"))
        if not events:
            raise ToolError(f"no events found in {p}")
        for ev in events:
            store.x("INSERT INTO events(title, start, end, location, source) VALUES (?,?,?,?,?)",
                    (ev["title"], ev["start"], ev.get("end"), ev.get("location"), str(p)))
        return {"file": str(p), "imported": len(events)}

    def time_entity(*keys: str):
        """Layer 2: a time that cannot be parsed is a missing detail -> ask before calling."""
        def ent(a: dict) -> list:
            out = []
            for k in keys:
                if a.get(k):
                    try:
                        parse_when(a[k], store.clock())
                        out.append(("time", a[k], True))
                    except ValueError:
                        out.append(("time", repr(a[k]), False))
            return out
        return ent

    def row_readback(table: str):
        def rb(a: dict, result) -> tuple[bool, str]:
            rows = store.q(f"SELECT id FROM {table} WHERE id=?", (result["id"],))
            return bool(rows), f"{table[:-1]} {result['id']} is stored" if rows else f"{table[:-1]} not found"
        return rb

    def forget_readback(a: dict, result) -> tuple[bool, str]:
        terms = [t for t in re.findall(r"\w+", a["topic"].lower()) if len(t) > 2]
        left = 0
        for table, col in (("facts", "text"), ("reminders", "text"), ("events", "title")):
            left += sum(1 for r in store.q(f"SELECT {col} AS t FROM {table}")
                        if all(t in r["t"].lower() for t in terms))
        return left == 0, "nothing about it remains" if left == 0 else f"{left} items still match"

    def import_readback(a: dict, result) -> tuple[bool, str]:
        n = store.q("SELECT count(*) AS n FROM events WHERE source=?", (result["file"],))[0]["n"]
        return n >= result["imported"], f"{n} events from {result['file']} are stored"

    return [
        Tool("add_reminder", A, "set a reminder; when is plain words like 'in 2 minutes', '18:30', 'tomorrow 9am'",
             {"text": S("what to remind about"), "when": S("when, in plain words")}, ["text", "when"],
             add_reminder, frozenset({Risk.WRITE}), entities=time_entity("when"),
             readback=row_readback("reminders"), changes_state=True),
        Tool("list_reminders", A, "list upcoming reminders", {"include_done": {"type": "boolean"}}, [],
             list_reminders),
        Tool("add_event", A, "add a calendar event; times in plain words like 'tomorrow 14:00'",
             {"title": S("event title"), "start": S("start time"), "end": S("end time"),
              "location": S("where")}, ["title", "start"], add_event, frozenset({Risk.WRITE}),
             entities=time_entity("start", "end"), readback=row_readback("events"), changes_state=True),
        Tool("list_events", A, "list calendar events for a day", {"day": S("today, tomorrow or YYYY-MM-DD")},
             [], list_events),
        Tool("remember", A, "remember a fact about the user for later", {"fact": S("the fact")}, ["fact"],
             remember, frozenset({Risk.WRITE}), readback=lambda a, r: row_readback("facts")(a, r),
             changes_state=True),
        Tool("recall", A, "look up what AIRA remembers, optionally about a topic", {"query": S("topic")}, [],
             recall),
        Tool("forget", A, "forget everything remembered about a topic", {"topic": S("topic to forget")},
             ["topic"], forget, frozenset({Risk.DELETE}), readback=forget_readback, changes_state=True),
        Tool("import_calendar", A, "import events from an .ics calendar file", {"path": S(".ics file path")},
             ["path"], import_calendar, frozenset({Risk.WRITE}),
             entities=lambda a: [exists_path(a["path"], "calendar file")], readback=import_readback,
             changes_state=True),
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


def register(reg: Registry, store: Store) -> None:
    for t in make_tools(store):
        reg.add(t)
