"""Parse human time expressions in code, so the model never computes timestamps.

Accepted: "in 5 minutes", "in 2 hours", "in 3 days", "in an hour", "in ten minutes",
"in half an hour", "18:30", "6pm", "6:15 pm",
"tomorrow 9am", "tomorrow at 09:00", "today 17:00", "monday 10am", ISO "2026-11-14T18:00",
"2026-11-14 18:00", "2026-11-14". Anything else raises ValueError (AIRA then asks the user).
"""
from __future__ import annotations

import datetime as dt
import re

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_REL = re.compile(r"^in\s+(\d+)\s*(minute|min|hour|hr|day|week)s?$")
NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
                "thirty": 30, "forty": 40, "forty-five": 45, "forty five": 45, "fifty": 50, "sixty": 60,
                "ninety": 90}


def _numbers_to_digits(s: str) -> str:
    """"in an hour" -> "in 1 hour", "in half an hour" -> "in 30 minutes" (relative times only)."""
    if re.fullmatch(r"in half an? hour", s):
        return "in 30 minutes"
    m = re.fullmatch(r"in (.+?) (minutes?|mins?|hours?|hrs?|days?|weeks?)", s)
    if m and m.group(1) in NUMBER_WORDS:
        return f"in {NUMBER_WORDS[m.group(1)]} {m.group(2)}"
    return s
_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def _clock(text: str) -> dt.time:
    m = _CLOCK.match(text.strip())
    if not m:
        raise ValueError(f"not a time of day: {text!r}")
    h, mi, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= h <= 12:
            raise ValueError(f"bad hour: {text!r}")
        h = (h % 12) + (12 if ampm == "pm" else 0)
    elif m.group(2) is None:
        raise ValueError(f"ambiguous time {text!r}: say e.g. 18:00 or 6pm")
    if not (0 <= h < 24 and 0 <= mi < 60):
        raise ValueError(f"bad time: {text!r}")
    return dt.time(h, mi)


def parse_when(text: str, now: dt.datetime | None = None) -> dt.datetime:
    now = (now or dt.datetime.now()).replace(second=0, microsecond=0)
    s = _numbers_to_digits(" ".join(text.lower().replace(" at ", " ").split()))
    m = _REL.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"minute": dt.timedelta(minutes=n), "min": dt.timedelta(minutes=n),
                 "hour": dt.timedelta(hours=n), "hr": dt.timedelta(hours=n),
                 "day": dt.timedelta(days=n), "week": dt.timedelta(weeks=n)}[unit]
        return now + delta
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return dt.datetime.combine(dt.date.fromisoformat(s), dt.time(9, 0))  # date only -> 09:00
    if re.match(r"^\d{4}-\d{2}-\d{2}[ t]\d{1,2}:\d{2}", s):
        try:
            return dt.datetime.fromisoformat(s[:10] + "T" + s[11:]).replace(tzinfo=None)
        except ValueError as exc:
            raise ValueError(f"bad date/time {text!r}") from exc
    parts = s.split(" ", 1)
    day_word, rest = (parts[0], parts[1] if len(parts) > 1 else "") if parts[0] in (
        ["today", "tomorrow", "tonight"] + WEEKDAYS) else ("", s)
    if day_word == "today" or day_word == "tonight" or day_word == "":
        base = now.date()
    elif day_word == "tomorrow":
        base = now.date() + dt.timedelta(days=1)
    else:
        ahead = (WEEKDAYS.index(day_word) - now.weekday()) % 7 or 7
        base = now.date() + dt.timedelta(days=ahead)
    if not rest:
        raise ValueError(f"no time of day in {text!r}")
    when = dt.datetime.combine(base, _clock(rest))
    if day_word == "" and when <= now:
        when += dt.timedelta(days=1)  # "18:00" when it is already past 18:00 means tomorrow
    return when
