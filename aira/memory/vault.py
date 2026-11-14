"""The memory vault: one SQLCipher-encrypted SQLite file.

Kinds and retention (CLAUDE.md):
  profile   until the user changes it (never auto-evicted)
  schedule  reminders and events; until 30 days after the event (future ones never auto-evicted)
  episode   summaries of past turns; after 90 days compacted to one line
  note      working notes; cleared at midnight
  action    action log; 30 days
Every item has an importance score: x0.95 per day, +1 when used (capped at 10). The nightly
`maintain(now)` purges, compacts, decays, backfills missing vectors and enforces the quota by
evicting the lowest-scored episodes first (then old notes/actions; never profile or future schedule).
Search: keyword (FTS5) + vector (sqlite-vec), merged with reciprocal-rank fusion.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import sqlcipher3
import sqlite_vec

from .embed import DIM, EmbedError

KINDS = ("profile", "schedule", "episode", "note", "action")
MiB = 1024 * 1024
QUOTA_MIN, QUOTA_DEFAULT, QUOTA_MAX = 64 * MiB, 256 * MiB, 2048 * MiB
HOT_CACHE_MAX = 32 * MiB
DECAY_PER_DAY = 0.95
USE_BOOST, IMPORTANCE_CAP = 1.0, 10.0
RETENTION = {"schedule_after_event": dt.timedelta(days=30), "episode_compact": dt.timedelta(days=90),
             "action": dt.timedelta(days=30)}
EPISODE_LINE = 120
# cosine distances measured with nomic-embed-text v1.5 (256 dims), 2026-11-14: related 0.12-0.30,
# unrelated >= 0.40. Search keeps vector hits up to 0.35; forget removes only up to 0.30.
SEARCH_MAX_DIST = 0.35
FORGET_MAX_DIST = 0.30

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ({",".join(repr(k) for k in KINDS)})),
  text TEXT NOT NULL,
  created TEXT NOT NULL,
  event_time TEXT,          -- schedule: reminder due time or event start
  meta TEXT NOT NULL DEFAULT '{{}}',
  importance REAL NOT NULL DEFAULT 1.0,
  last_used TEXT,
  compacted INTEGER NOT NULL DEFAULT 0,
  has_vec INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_kind ON items(kind, event_time);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(text, content='items', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE OF text ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO items_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE VIRTUAL TABLE IF NOT EXISTS items_vec USING vec0(embedding float[{DIM}] distance_metric=cosine);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, label TEXT NOT NULL, pid INTEGER, unit TEXT, started TEXT NOT NULL,
  finished TEXT, status TEXT, notified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def parse(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s) if s else None


@dataclass
class Item:
    id: int
    kind: str
    text: str
    created: str
    event_time: str | None
    meta: dict
    importance: float
    compacted: bool

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "text": self.text, "created": self.created,
                "event_time": self.event_time, "importance": round(self.importance, 3), **self.meta}


class VaultLocked(RuntimeError):
    pass


class Vault:
    def __init__(self, path: Path, key: str, embedder=None, clock: Callable[[], dt.datetime] = dt.datetime.now,
                 quota_bytes: int | None = None):
        self.path = Path(path)
        self.embedder = embedder
        self.clock = clock
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self.db = sqlcipher3.connect(str(self.path), check_same_thread=False)
        # PRAGMA cannot take bound parameters: a 64-hex key is used as a raw key, anything else
        # as a passphrase with quotes escaped
        if re.fullmatch(r"[0-9a-f]{64}", key):
            self.db.execute(f"PRAGMA key = \"x'{key}'\"")
        else:
            self.db.execute("PRAGMA key = '" + key.replace("'", "''") + "'")
        try:
            self.db.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlcipher3.DatabaseError as exc:
            raise VaultLocked(f"cannot open the vault {self.path}: wrong key or not a vault") from exc
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        if new:
            os.chmod(self.path, 0o600)
        if quota_bytes is not None:
            self.set_quota(quota_bytes)
        self.hot: dict = {}
        self.refresh_hot()

    # ------------------------------------------------------------- settings
    def setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self.db.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE "
                            "SET value=excluded.value", (key, value))
            self.db.commit()

    @property
    def quota(self) -> int:
        return int(self.setting("quota_bytes", str(QUOTA_DEFAULT)))

    def set_quota(self, n: int) -> None:
        if not QUOTA_MIN <= n <= QUOTA_MAX:
            raise ValueError(f"quota must be between 64 MB and 2 GB, not {n / MiB:.0f} MB")
        self.set_setting("quota_bytes", str(n))

    @property
    def paused(self) -> bool:
        return self.setting("paused", "0") == "1"

    def set_paused(self, on: bool) -> None:
        self.set_setting("paused", "1" if on else "0")

    def _pragma_int(self, name: str) -> int:
        # SQLCipher returns some pragmas (page_size) as strings
        with self._lock:
            return int(self.db.execute(f"PRAGMA {name}").fetchone()[0])

    def size_bytes(self) -> int:
        """Size of the vault file (pages, including free ones)."""
        return self._pragma_int("page_count") * self._pragma_int("page_size")

    def used_bytes(self) -> int:
        """Bytes in use (excluding free pages): what the quota measures."""
        return (self._pragma_int("page_count") - self._pragma_int("freelist_count")) * self._pragma_int("page_size")

    # ------------------------------------------------------------- writing
    def add(self, kind: str, text: str, *, event_time: dt.datetime | None = None, meta: dict | None = None,
            importance: float = 1.0, embed: bool = True) -> int:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind}")
        text = text.strip()
        if not text:
            raise ValueError("empty text")
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO items(kind, text, created, event_time, meta, importance) VALUES (?,?,?,?,?,?)",
                (kind, text, iso(self.clock()), iso(event_time) if event_time else None,
                 json.dumps(meta or {}), importance))
            iid = cur.lastrowid
            if embed and kind != "action":
                self._embed_ids([(iid, text)])
            self.db.commit()
        if kind in ("profile", "schedule"):
            self.refresh_hot()
        return iid

    def add_many(self, rows: list[tuple[str, str]], embed: bool = True) -> None:
        """Bulk insert (kind, text) rows; used for imports and tests."""
        with self._lock:
            now = iso(self.clock())
            ids = []
            for kind, text in rows:
                cur = self.db.execute("INSERT INTO items(kind, text, created) VALUES (?,?,?)", (kind, text, now))
                ids.append((cur.lastrowid, text))
            if embed:
                self._embed_ids([x for x, (k, _) in zip(ids, rows) if k != "action"])
            self.db.commit()

    def _embed_ids(self, rows: list[tuple[int, str]]) -> None:
        if not rows or self.embedder is None:
            return
        try:
            vecs = []
            for i in range(0, len(rows), 64):
                vecs += self.embedder.documents([t for _, t in rows[i:i + 64]])
        except EmbedError:
            return  # stored without a vector; maintain() backfills later
        for (iid, _), v in zip(rows, vecs):
            self.db.execute("INSERT OR REPLACE INTO items_vec(rowid, embedding) VALUES (?, ?)",
                            (iid, sqlite_vec.serialize_float32(v)))
            self.db.execute("UPDATE items SET has_vec=1 WHERE id=?", (iid,))

    def update(self, iid: int, **fields) -> None:
        allowed = {"text", "event_time", "meta", "importance", "compacted"}
        if not set(fields) <= allowed:
            raise ValueError(f"cannot update {set(fields) - allowed}")
        with self._lock:
            if "meta" in fields:
                fields["meta"] = json.dumps(fields["meta"])
            if "event_time" in fields and isinstance(fields["event_time"], dt.datetime):
                fields["event_time"] = iso(fields["event_time"])
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), iid))
            if "text" in fields:
                self.db.execute("DELETE FROM items_vec WHERE rowid=?", (iid,))
                self.db.execute("UPDATE items SET has_vec=0 WHERE id=?", (iid,))
                self._embed_ids([(iid, fields["text"])])
            self.db.commit()
        self.refresh_hot()

    def delete(self, ids: list[int]) -> int:
        if not ids:
            return 0
        with self._lock:
            q = ",".join("?" * len(ids))
            self.db.execute(f"DELETE FROM items_vec WHERE rowid IN ({q})", ids)
            n = self.db.execute(f"DELETE FROM items WHERE id IN ({q})", ids).rowcount
            self.db.commit()
        self.refresh_hot()
        return n

    # ------------------------------------------------------------- reading
    def _rows(self, sql: str, params: tuple = ()) -> list[Item]:
        out = []
        with self._lock:
            rows = self.db.execute(sql, params).fetchall()
        for r in rows:
            out.append(Item(r[0], r[1], r[2], r[3], r[4], json.loads(r[5]), r[6], bool(r[7])))
        return out

    _COLS = "id, kind, text, created, event_time, meta, importance, compacted"

    def get(self, iid: int) -> Item | None:
        rows = self._rows(f"SELECT {self._COLS} FROM items WHERE id=?", (iid,))
        return rows[0] if rows else None

    def by_kind(self, kind: str, where: str = "", params: tuple = (), order: str = "id") -> list[Item]:
        return self._rows(f"SELECT {self._COLS} FROM items WHERE kind=? {where} ORDER BY {order}", (kind, *params))

    def touch(self, ids: list[int]) -> None:
        """Importance rises on use."""
        if not ids:
            return
        with self._lock:
            q = ",".join("?" * len(ids))
            self.db.execute(f"UPDATE items SET importance=min(importance+?, ?), last_used=? WHERE id IN ({q})",
                            (USE_BOOST, IMPORTANCE_CAP, iso(self.clock()), *ids))
            self.db.commit()

    @staticmethod
    def _fts_query(text: str) -> str | None:
        terms = [t for t in re.findall(r"\w+", text.lower()) if len(t) > 1]
        return " AND ".join(f'"{t}"' for t in terms) if terms else None

    def keyword_search(self, query: str, k: int = 10, kinds: tuple[str, ...] = KINDS) -> list[Item]:
        q = self._fts_query(query)
        if not q:
            return []
        kq = ",".join("?" * len(kinds))
        return self._rows(
            f"SELECT {', '.join('i.' + c.strip() for c in self._COLS.split(','))} FROM items_fts f "
            f"JOIN items i ON i.id=f.rowid WHERE items_fts MATCH ? AND i.kind IN ({kq}) "
            f"ORDER BY bm25(items_fts) LIMIT ?", (q, *kinds, k))

    def vector_search(self, query: str, k: int = 10, max_distance: float | None = None,
                      kinds: tuple[str, ...] = KINDS) -> list[tuple[Item, float]]:
        if self.embedder is None:
            return []
        try:
            v = self.embedder.query(query)
        except EmbedError:
            return []
        with self._lock:
            hits = self.db.execute("SELECT rowid, distance FROM items_vec WHERE embedding MATCH ? AND k = ? "
                                   "ORDER BY distance", (sqlite_vec.serialize_float32(v), k * 3)).fetchall()
        out = []
        for rowid, dist in hits:
            if max_distance is not None and dist > max_distance:
                continue
            it = self.get(rowid)
            if it and it.kind in kinds:
                out.append((it, dist))
        return out[:k]

    def search(self, query: str, k: int = 8, kinds: tuple[str, ...] = KINDS, touch: bool = True) -> list[Item]:
        """Hybrid search with reciprocal-rank fusion; marks results as used."""
        scores: dict[int, float] = {}
        items: dict[int, Item] = {}
        for rank, it in enumerate(self.keyword_search(query, k * 2, kinds)):
            scores[it.id] = scores.get(it.id, 0) + 1 / (60 + rank)
            items[it.id] = it
        for rank, (it, _) in enumerate(self.vector_search(query, k * 2, max_distance=SEARCH_MAX_DIST, kinds=kinds)):
            scores[it.id] = scores.get(it.id, 0) + 1 / (60 + rank)
            items[it.id] = it
        best = sorted(scores, key=scores.get, reverse=True)[:k]
        if touch:
            self.touch(best)
        return [items[i] for i in best]

    # ------------------------------------------------------------- forget / export
    def forget(self, topic: str, vector_distance: float = FORGET_MAX_DIST) -> list[Item]:
        """Remove items about a topic from both indexes: every keyword match (all words present),
        plus vector matches that are very close (cosine distance <= vector_distance)."""
        hits = {it.id: it for it in self.keyword_search(topic, k=10_000)}
        for it, _ in self.vector_search(topic, k=200, max_distance=vector_distance):
            hits.setdefault(it.id, it)
        removed = [it for it in hits.values() if it.kind != "action"]
        self.delete([it.id for it in removed])
        self._redact_actions(topic)
        return removed

    def _redact_actions(self, topic: str) -> int:
        """Keep the audit record of actions (time, tool, risk, decision) but replace any argument
        or message that mentions the topic with "[forgotten]"."""
        terms = [t for t in re.findall(r"\w+", topic.lower()) if len(t) > 1]
        if not terms:
            return 0

        def scrub(v):
            if isinstance(v, str):
                return "[forgotten]" if any(t in v.lower() for t in terms) else v
            if isinstance(v, dict):
                return {k: scrub(x) for k, x in v.items()}
            if isinstance(v, list):
                return [scrub(x) for x in v]
            return v

        candidates = {it.id: it for t in terms for it in self.keyword_search(t, k=100_000, kinds=("action",))}
        n = 0
        for it in candidates.values():
            try:
                entry = json.loads(it.text)
            except ValueError:
                entry = it.text
            new = scrub(entry) if isinstance(entry, str) else json.dumps(scrub(entry))
            if new != it.text:
                with self._lock:
                    self.db.execute("UPDATE items SET text=? WHERE id=?", (new, it.id))
                    self.db.commit()
                n += 1
        return n

    def topic_present(self, topic: str, vector_distance: float = FORGET_MAX_DIST) -> dict:
        """Read-back for forget: does anything about the topic remain in either search?"""
        kw = [it.id for it in self.keyword_search(topic, k=50, kinds=KINDS[:4])]
        vec = [it.id for it, _ in self.vector_search(topic, k=50, max_distance=vector_distance, kinds=KINDS[:4])]
        return {"keyword": kw, "vector": vec}

    def export(self, dest: Path) -> dict:
        items = [it.to_dict() for it in self._rows(f"SELECT {self._COLS} FROM items ORDER BY id")]
        jobs = [dict(zip(("id", "label", "pid", "unit", "started", "finished", "status"), r))
                for r in self.db.execute("SELECT id, label, pid, unit, started, finished, status FROM jobs")]
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"exported": iso(self.clock()), "items": items, "jobs": jobs}, f, indent=2)
        return {"file": str(dest), "items": len(items), "jobs": len(jobs)}

    # ------------------------------------------------------------- schedule helpers
    def due_reminders(self, now: dt.datetime) -> list[Item]:
        return [it for it in self.by_kind("schedule", "AND event_time <= ?", (iso(now),), "event_time")
                if it.meta.get("type") == "reminder" and not it.meta.get("fired")]

    def day_schedule(self, day: dt.date) -> list[Item]:
        return self.by_kind("schedule", "AND substr(event_time,1,10)=?", (day.isoformat(),), "event_time")

    def open_reminders(self, now: dt.datetime) -> list[Item]:
        return [it for it in self.by_kind("schedule", order="event_time")
                if it.meta.get("type") == "reminder" and not it.meta.get("fired")]

    # ------------------------------------------------------------- hot cache
    def refresh_hot(self) -> None:
        """RAM cache: profile, today's schedule, open tasks. Trimmed to stay under 32 MB."""
        now = self.clock()
        profile = [it.to_dict() for it in self.by_kind("profile", order="importance DESC")]
        today = [it.to_dict() for it in self.day_schedule(now.date())]
        tasks = [it.to_dict() for it in self.open_reminders(now)]
        hot = {"profile": profile, "today": today, "open_tasks": tasks}
        while len(json.dumps(hot)) > HOT_CACHE_MAX and (hot["profile"] or hot["open_tasks"]):
            (hot["open_tasks"] or hot["profile"]).pop()
        self.hot = hot

    def hot_size(self) -> int:
        return len(json.dumps(self.hot).encode())

    # ------------------------------------------------------------- nightly maintenance
    def maintain(self, now: dt.datetime | None = None) -> dict:
        now = now or self.clock()
        report = {"purged_schedule": 0, "cleared_notes": 0, "purged_actions": 0, "compacted": 0,
                  "decayed_days": 0, "backfilled": 0, "evicted": 0, "size_before": self.size_bytes()}
        with self._lock:
            # retention
            cutoff = iso(now - RETENTION["schedule_after_event"])
            old = [r[0] for r in self.db.execute(
                "SELECT id FROM items WHERE kind='schedule' AND event_time IS NOT NULL AND event_time < ?", (cutoff,))]
            report["purged_schedule"] = self.delete(old)
            midnight = iso(dt.datetime.combine(now.date(), dt.time(0, 0)))
            notes = [r[0] for r in self.db.execute("SELECT id FROM items WHERE kind='note' AND created < ?",
                                                   (midnight,))]
            report["cleared_notes"] = self.delete(notes)
            acts = [r[0] for r in self.db.execute("SELECT id FROM items WHERE kind='action' AND created < ?",
                                                  (iso(now - RETENTION["action"]),))]
            report["purged_actions"] = self.delete(acts)
            # compaction: episodes older than 90 days become one line
            for iid, text in self.db.execute(
                    "SELECT id, text FROM items WHERE kind='episode' AND compacted=0 AND created < ?",
                    (iso(now - RETENTION["episode_compact"]),)).fetchall():
                line = " ".join(text.split("\n", 1)[0].split())
                if len(line) > EPISODE_LINE:
                    line = line[: EPISODE_LINE - 3].rstrip() + "..."
                self.update(iid, text=line, compacted=1)
                report["compacted"] += 1
            # decay, once per elapsed day
            last = parse(self.setting("last_decay")) or now
            days = (now.date() - last.date()).days
            if days > 0:
                self.db.execute("UPDATE items SET importance = importance * ?", (DECAY_PER_DAY ** days,))
                report["decayed_days"] = days
            self.set_setting("last_decay", iso(now))
            # backfill vectors that could not be computed earlier
            missing = self.db.execute("SELECT id, text FROM items WHERE has_vec=0 AND kind!='action' LIMIT 2000"
                                      ).fetchall()
            before = self.db.execute("SELECT count(*) FROM items WHERE has_vec=1").fetchone()[0]
            self._embed_ids(missing)
            report["backfilled"] = self.db.execute("SELECT count(*) FROM items WHERE has_vec=1").fetchone()[0] - before
            self.db.commit()
            # quota
            report["evicted"] = self._enforce_quota(now)
        report["size_after"] = self.size_bytes()
        report["quota"] = self.quota
        self.refresh_hot()
        return report

    def _enforce_quota(self, now: dt.datetime) -> int:
        if self.used_bytes() <= self.quota:
            if self.size_bytes() > self.quota:
                self.db.execute("VACUUM")  # free pages only: give them back
            return 0
        evicted = 0
        # lowest-scored episodes first, then old notes and actions; never profile or future schedule
        order = [("episode", ""), ("note", ""), ("action", ""),
                 ("schedule", "AND event_time < ?")]
        for kind, extra in order:
            while self.used_bytes() > self.quota:
                params = (kind, iso(now)) if extra else (kind,)
                ids = [r[0] for r in self.db.execute(
                    f"SELECT id FROM items WHERE kind=? {extra} ORDER BY importance ASC, created ASC LIMIT 500",
                    params)]
                if not ids:
                    break
                evicted += self.delete(ids)
            if self.used_bytes() <= self.quota:
                break
        self.db.execute("VACUUM")
        return evicted

    def close(self) -> None:
        self.db.close()
