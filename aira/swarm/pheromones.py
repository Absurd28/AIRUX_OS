"""Pheromone board: a small SQLite file where workers leave a score per approach.

A success deposits more when it was fast (1 / (1 + seconds)); a failure takes 1 away. Scores
decay by DECAY per day, so old experience fades. Agents and workers try approaches in order of
score; an approach never tried starts at UNTRIED so it gets a chance. Holds no personal data
(task kinds, approach names and numbers only), so it is a plain file next to the vault.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from pathlib import Path
from typing import Callable

DECAY = 0.9          # per day
UNTRIED = 0.5
FAIL_PENALTY = 1.0


class PheromoneBoard:
    def __init__(self, path: Path, clock: Callable[[], dt.datetime] = dt.datetime.now):
        self.clock = clock
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS trails (task TEXT, approach TEXT, score REAL NOT NULL, "
                        "updated TEXT NOT NULL, successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL "
                        "DEFAULT 0, PRIMARY KEY (task, approach))")
        self.db.commit()

    def _decayed(self, score: float, updated: str) -> float:
        days = (self.clock() - dt.datetime.fromisoformat(updated)).total_seconds() / 86400
        return score * (DECAY ** max(0.0, days))

    def score(self, task: str, approach: str) -> float:
        with self._lock:
            r = self.db.execute("SELECT score, updated FROM trails WHERE task=? AND approach=?",
                                (task, approach)).fetchone()
        return UNTRIED if r is None else self._decayed(*r)

    def deposit(self, task: str, approach: str, success: bool, seconds: float) -> float:
        amount = (1.0 / (1.0 + max(0.0, seconds))) if success else -FAIL_PENALTY
        new = self.score(task, approach) + amount
        now = self.clock().isoformat(timespec="seconds")
        with self._lock:
            self.db.execute(
                "INSERT INTO trails(task, approach, score, updated, successes, failures) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(task, approach) DO UPDATE SET score=excluded.score, updated=excluded.updated, "
                "successes=successes+excluded.successes, failures=failures+excluded.failures",
                (task, approach, new, now, int(success), int(not success)))
            self.db.commit()
        return new

    def ranked(self, task: str, approaches: list[str]) -> list[str]:
        """Approaches, highest score first (ties keep the given order)."""
        return sorted(approaches, key=lambda a: -self.score(task, a))

    def table(self, task: str | None = None) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT task, approach, score, updated, successes, failures FROM trails"
                                   + (" WHERE task=?" if task else ""), (task,) if task else ()).fetchall()
        return [{"task": t, "approach": a, "score": round(self._decayed(s, u), 4), "successes": ok, "failures": bad}
                for t, a, s, u, ok, bad in rows]
