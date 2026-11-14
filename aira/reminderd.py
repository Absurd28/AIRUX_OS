"""Reminder daemon (systemd user service): python -m aira.reminderd [--briefing]

Every 30 seconds:
  - due reminders: desktop notification + spoken by AIRA; if the screen is locked, repeated at unlock
  - watched jobs (pid or systemd unit) that finished or failed: notification + speech
  - once a day after 03:00: the vault's nightly maintenance
--briefing prints and speaks the login briefing once and exits (aira-briefing.service).
All activity is appended to <data>/reminderd.log as JSON lines.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .config import Config
from .memory.vault import Vault, iso

INTERVAL_S = 30
MAINTENANCE_HOUR = 3


def screen_locked() -> bool | None:
    """True/False if a lock state is available (logind LockedHint, then the ScreenSaver D-Bus API)."""
    sid = os.environ.get("XDG_SESSION_ID")
    try:
        if sid:
            r = subprocess.run(["loginctl", "show-session", sid, "-p", "LockedHint", "--value"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip() in ("yes", "no"):
                return r.stdout.strip() == "yes"
        r = subprocess.run(["gdbus", "call", "--session", "--timeout", "3", "--dest", "org.freedesktop.ScreenSaver",
                            "--object-path", "/org/freedesktop/ScreenSaver", "--method",
                            "org.freedesktop.ScreenSaver.GetActive"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return "true" in r.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def job_state(pid: int | None, unit: str | None) -> tuple[str, str] | None:
    """('running'|'finished'|'failed', detail), or None if it cannot be known."""
    if unit:
        r = subprocess.run(["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "Result",
                            "-p", "LoadState"], capture_output=True, text=True, timeout=10)
        props = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        if not props or props.get("LoadState") == "not-found":
            return None
        active, result = props.get("ActiveState", ""), props.get("Result", "")
        if active in ("active", "activating", "reloading", "deactivating"):
            return "running", active
        return ("finished", "success") if result in ("success", "") else ("failed", result)
    if pid:
        if Path(f"/proc/{pid}").exists():
            return "running", f"pid {pid}"
        return "finished", f"pid {pid} exited (exit code unknown for a process AIRA did not start)"
    return None


class Daemon:
    def __init__(self, vault: Vault, notify: Callable, speak: Callable, user_name: str = "",
                 locked: Callable[[], bool | None] = screen_locked, log_path: Path | None = None,
                 jobs: Callable = job_state):
        self.vault, self.notify, self.speak = vault, notify, speak
        self.user_name, self.locked, self.jobs = user_name, locked, jobs
        self.log_path = log_path
        self.was_locked = False

    def log(self, event: str, **kw) -> None:
        if self.log_path:
            with self.log_path.open("a") as f:
                f.write(json.dumps({"ts": iso(self.vault.clock()), "event": event, **kw}, default=str) + "\n")

    def _announce(self, title: str, text: str, spoken: str) -> dict:
        ok, detail = self.notify(title, text)
        voice = self.speak(spoken)
        return {"notified": ok, "notify_detail": detail, **voice}

    def tick(self, now: dt.datetime | None = None) -> list[dict]:
        now = now or self.vault.clock()
        events = []
        locked = bool(self.locked())
        name = f", {self.user_name}" if self.user_name else ""
        # due reminders
        for it in self.vault.due_reminders(now):
            res = self._announce("AIRA reminder", it.text, f"Reminder{name}: {it.text}")
            meta = {**it.meta, "fired": iso(now), "fired_locked": locked, **({"repeat_at_unlock": True} if locked else {})}
            self.vault.update(it.id, meta=meta)
            ev = {"event": "reminder", "id": it.id, "text": it.text, "due": it.event_time, "locked": locked, **res}
            self.log(**ev)
            events.append(ev)
        # repeat at unlock
        if self.was_locked and not locked:
            for it in self.vault.by_kind("schedule"):
                if it.meta.get("repeat_at_unlock"):
                    res = self._announce("AIRA reminder (while you were away)", it.text,
                                         f"While you were away{name}: {it.text}")
                    self.vault.update(it.id, meta={**it.meta, "repeat_at_unlock": False, "repeated": iso(now)})
                    ev = {"event": "reminder_repeat", "id": it.id, "text": it.text, **res}
                    self.log(**ev)
                    events.append(ev)
        self.was_locked = locked
        # watched jobs
        for jid, label, pid, unit in self.vault.db.execute(
                "SELECT id, label, pid, unit FROM jobs WHERE finished IS NULL").fetchall():
            st = self.jobs(pid, unit)
            if st is None or st[0] == "running":
                continue
            self.vault.db.execute("UPDATE jobs SET finished=?, status=?, notified=1 WHERE id=?",
                                  (iso(now), f"{st[0]}: {st[1]}", jid))
            self.vault.db.commit()
            res = self._announce(f"AIRA: job {st[0]}", f"{label}: {st[1]}", f"{label} has {st[0]}.")
            ev = {"event": "job", "id": jid, "label": label, "state": st[0], **res}
            self.log(**ev)
            events.append(ev)
        # nightly maintenance, once per day after 03:00
        if now.hour >= MAINTENANCE_HOUR and self.vault.setting("last_maintenance", "")[:10] != now.date().isoformat():
            report = self.vault.maintain(now)
            self.vault.set_setting("last_maintenance", iso(now))
            ev = {"event": "maintenance", **report}
            self.log(**ev)
            events.append(ev)
        return events


def briefing(vault: Vault, now: dt.datetime, user_name: str = "") -> dict:
    """Today's events, due reminders, watched jobs that finished or failed since the last briefing."""
    today = [it for it in vault.day_schedule(now.date()) if it.meta.get("type") == "event"]
    due = [it for it in vault.open_reminders(now)
           if it.event_time and dt.datetime.fromisoformat(it.event_time).date() <= now.date()]
    since = vault.setting("last_briefing", "1970-01-01T00:00:00")
    jobs = vault.db.execute("SELECT label, status, finished FROM jobs WHERE finished IS NOT NULL AND finished > ? "
                            "ORDER BY finished", (since,)).fetchall()
    lines = []
    name = f", {user_name}" if user_name else ""
    lines.append(f"Good {'morning' if now.hour < 12 else 'afternoon' if now.hour < 18 else 'evening'}{name}.")
    if today:
        lines.append(f"Today you have {len(today)} event{'s' if len(today) != 1 else ''}: " + "; ".join(
            f"{it.text} at {it.event_time[11:16]}" for it in today) + ".")
    else:
        lines.append("You have no events today.")
    if due:
        lines.append(f"{len(due)} reminder{'s are' if len(due) != 1 else ' is'} due today: " + "; ".join(
            f"{it.text} at {it.event_time[11:16]}" for it in due) + ".")
    for label, status, _ in jobs:
        lines.append(f"Watched job {label} {status.split(':')[0]}.")
    vault.set_setting("last_briefing", iso(now))
    return {"text": " ".join(lines), "events": [it.to_dict() for it in today],
            "reminders": [it.to_dict() for it in due], "jobs": [{"label": l, "status": s} for l, s, _ in jobs]}


def open_vault(cfg: Config) -> Vault:
    from .memory.embed import Embedder
    from .memory.keys import get_key
    key, _ = get_key()
    return Vault(cfg.vault_path, key, embedder=Embedder())


def main(argv: list[str] | None = None) -> int:
    from .notify import notify
    from .tts import Speaker
    ap = argparse.ArgumentParser(prog="aira.reminderd")
    ap.add_argument("--briefing", action="store_true")
    ap.add_argument("--interval", type=float, default=INTERVAL_S)
    args = ap.parse_args(argv)
    cfg = Config()
    cfg.ensure_dirs()
    vault = open_vault(cfg)
    speaker = Speaker(out_dir=cfg.data_dir / "speech")
    if args.briefing:
        b = briefing(vault, dt.datetime.now(), cfg.user_name)
        print(b["text"])
        notify("AIRA briefing", b["text"])
        speaker.say(b["text"])
        return 0
    d = Daemon(vault, notify, speaker.say, cfg.user_name, log_path=cfg.data_dir / "reminderd.log")
    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    d.log("start", interval=args.interval, pid=os.getpid())
    print(f"aira-reminderd: checking every {args.interval:.0f}s", flush=True)
    while not stop:
        try:
            for ev in d.tick():
                print(json.dumps(ev, default=str), flush=True)
        except Exception as exc:  # keep running; report plainly
            d.log("error", error=f"{type(exc).__name__}: {exc}")
            print(f"error: {exc}", file=sys.stderr, flush=True)
        for _ in range(int(args.interval * 10)):
            if stop:
                break
            time.sleep(0.1)
    d.log("stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
