"""Protection history, allowed files and settings, in one SQLite file (root-only)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, level TEXT, threat TEXT, path TEXT,
  pid INTEGER, exe TEXT, engine TEXT, action TEXT, detail TEXT, uid INTEGER);
CREATE INDEX IF NOT EXISTS history_ts ON history(ts);
CREATE TABLE IF NOT EXISTS allowed (sha256 TEXT PRIMARY KEY, path TEXT, threat TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""
KEEP_DAYS = 90


class Store:
    def __init__(self, path: str | Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def event(self, kind: str, **kw) -> int:
        cols = ["ts", "kind"] + [k for k in ("level", "threat", "path", "pid", "exe", "engine", "action", "detail", "uid")
                                 if k in kw]
        vals = [self.clock(), kind] + [json.dumps(kw[c]) if c == "detail" and not isinstance(kw[c], str) else kw[c]
                                      for c in cols[2:]]
        with self._lock:
            cur = self.db.execute(f"INSERT INTO history ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            self.db.commit()
            return cur.lastrowid

    def set_action(self, event_id: int, action: str) -> None:
        with self._lock:
            self.db.execute("UPDATE history SET action=? WHERE id=?", (action, event_id))
            self.db.commit()

    def events(self, limit: int = 100, kinds: tuple[str, ...] | None = None) -> list[dict]:
        q = "SELECT * FROM history"
        args: list = []
        if kinds:
            q += f" WHERE kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            cur = self.db.execute(q, args)
            names = [c[0] for c in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def prune(self) -> int:
        with self._lock:
            n = self.db.execute("DELETE FROM history WHERE ts < ?", (self.clock() - KEEP_DAYS * 86400,)).rowcount
            self.db.commit()
            return n

    # ---- allowed files (exclusions), by exact content
    def allow(self, sha256: str, path: str, threat: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO allowed VALUES (?,?,?,?)", (sha256, path, threat, self.clock()))
            self.db.commit()

    def unallow(self, sha256: str) -> bool:
        with self._lock:
            n = self.db.execute("DELETE FROM allowed WHERE sha256=?", (sha256,)).rowcount
            self.db.commit()
            return n > 0

    def is_allowed(self, sha256: str) -> bool:
        with self._lock:
            return self.db.execute("SELECT 1 FROM allowed WHERE sha256=?", (sha256,)).fetchone() is not None

    def allowed(self) -> list[dict]:
        with self._lock:
            return [dict(zip(("sha256", "path", "threat", "ts"), r))
                    for r in self.db.execute("SELECT * FROM allowed ORDER BY ts DESC")]

    # ---- settings
    def get(self, key: str, default=None):
        with self._lock:
            r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, key: str, value) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
            self.db.commit()
