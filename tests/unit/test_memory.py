"""Memory vault and reminder daemon tests with a fake clock (no model server needed)."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import stat

import pytest

from aira.memory.embed import HashEmbedder
from aira.memory.vault import (EPISODE_LINE, HOT_CACHE_MAX, QUOTA_MIN, Vault, VaultLocked)
from aira.persona import Persona
from aira.reminderd import Daemon, briefing


class Clock:
    def __init__(self, t: dt.datetime):
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += dt.timedelta(**kw)


T0 = dt.datetime(2026, 11, 14, 10, 0)


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def vault(tmp_path, clock):
    v = Vault(tmp_path / "vault.db", "a" * 64, embedder=HashEmbedder(), clock=clock)
    yield v
    v.close()


# ---------- encryption ----------

def test_vault_is_encrypted(tmp_path, clock):
    v = Vault(tmp_path / "v.db", "b" * 64, embedder=HashEmbedder(), clock=clock)
    v.add("profile", "secret thesis topic: sparse attention")
    v.close()
    raw = (tmp_path / "v.db").read_bytes()
    assert b"sparse attention" not in raw and not raw.startswith(b"SQLite format 3")
    assert stat.S_IMODE(os.stat(tmp_path / "v.db").st_mode) == 0o600
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(tmp_path / "v.db").execute("SELECT * FROM items").fetchall()
    with pytest.raises(VaultLocked):
        Vault(tmp_path / "v.db", "c" * 64, clock=clock)
    assert Vault(tmp_path / "v.db", "b" * 64, clock=clock).by_kind("profile")[0].text.startswith("secret")


# ---------- search and forget ----------

def test_forget_removes_from_both_searches(vault):
    vault.add("profile", "Project Falcon uses PyTorch 2.5")
    vault.add("episode", "asked about project falcon training speed")
    vault.add("profile", "I prefer dark mode in editors")
    assert vault.keyword_search("falcon") and vault.vector_search("falcon", max_distance=0.6)
    removed = vault.forget("falcon")
    assert len(removed) == 2
    left = vault.topic_present("falcon")
    assert left == {"keyword": [], "vector": []}
    assert not vault.keyword_search("falcon")
    assert all("falcon" not in it.text.lower() for it, _ in vault.vector_search("falcon", k=10))
    assert [it.text for it in vault.by_kind("profile")] == ["I prefer dark mode in editors"]


def test_hybrid_search_and_use_boost(vault):
    iid = vault.add("profile", "My thesis deadline is March 3rd")
    vault.add("profile", "I like green tea")
    hits = vault.search("thesis deadline")
    assert hits and hits[0].id == iid
    assert vault.get(iid).importance == pytest.approx(2.0)  # 1.0 + use boost


# ---------- retention with a fake clock ----------

def test_retention_rules(vault, clock):
    old_event = vault.add("schedule", "conference", event_time=T0 - dt.timedelta(days=31), meta={"type": "event"})
    recent_event = vault.add("schedule", "seminar", event_time=T0 - dt.timedelta(days=29), meta={"type": "event"})
    future = vault.add("schedule", "defense", event_time=T0 + dt.timedelta(days=60), meta={"type": "event"})
    note = vault.add("note", "working on chapter 2")
    long_text = "Discussed the transformer divergence at step 400. " + "Details: " + "x " * 200 + "\nSecond line words zebra"
    ep = vault.add("episode", long_text)
    act = vault.add("action", json.dumps({"tool": "disk_usage"}), embed=False)
    prof = vault.add("profile", "Name: Absurd")

    clock.advance(days=1)                     # past midnight: working notes cleared
    r = vault.maintain()
    assert vault.get(note) is None and r["cleared_notes"] == 1
    assert vault.get(old_event) is None and vault.get(recent_event) is not None
    assert vault.get(ep) is not None and not vault.get(ep).compacted

    clock.advance(days=30)                    # day 31: action log entry (31 days old) purged
    vault.maintain()
    assert vault.get(act) is None
    assert vault.get(recent_event) is None    # now 30+ days after the event

    clock.advance(days=60)                    # day 91: episode compacted to one line
    r = vault.maintain()
    e = vault.get(ep)
    assert r["compacted"] == 1 and e.compacted and "\n" not in e.text and len(e.text) <= EPISODE_LINE
    assert vault.keyword_search("transformer divergence") and not vault.keyword_search("zebra")
    assert vault.get(prof) is not None        # profile kept
    assert vault.get(future) is not None or T0 + dt.timedelta(days=60) + dt.timedelta(days=30) < clock()


def test_decay_and_boost(vault, clock):
    iid = vault.add("episode", "ran the tests")
    vault.maintain()
    clock.advance(days=10)
    vault.maintain()
    assert vault.get(iid).importance == pytest.approx(0.95 ** 10)
    vault.touch([iid])
    assert vault.get(iid).importance == pytest.approx(0.95 ** 10 + 1)


def test_quota_evicts_lowest_scored_episodes_only(tmp_path, clock):
    v = Vault(tmp_path / "q.db", "d" * 64, embedder=None, clock=clock, quota_bytes=QUOTA_MIN)
    profile = v.add("profile", "Name: Absurd", embed=False)
    future = v.add("schedule", "thesis defense", event_time=T0 + dt.timedelta(days=90),
                   meta={"type": "event"}, embed=False)
    keep = v.add("episode", "important: the group meeting decision", importance=9.0, embed=False)
    filler = "lorem ipsum " * 170  # ~2 KB per episode
    for _ in range(18):
        v.add_many([("episode", f"episode {i} {filler}") for i in range(2000)], embed=False)
    assert v.used_bytes() > QUOTA_MIN
    r = v.maintain()
    assert r["evicted"] > 0
    assert v.used_bytes() <= QUOTA_MIN and v.size_bytes() <= QUOTA_MIN
    assert v.get(profile) and v.get(future) and v.get(keep)   # never evicted / highest score kept
    with pytest.raises(ValueError):
        v.set_quota(32 * 1024 * 1024)
    v.close()


def test_hot_cache(vault, clock):
    vault.add("profile", "Name: Absurd")
    vault.add("schedule", "standup", event_time=T0 + dt.timedelta(hours=2), meta={"type": "event"})
    vault.add("schedule", "call mum", event_time=T0 + dt.timedelta(days=3), meta={"type": "reminder"})
    vault.add("schedule", "old", event_time=T0 - dt.timedelta(days=3), meta={"type": "event"})
    h = vault.hot
    assert [p["text"] for p in h["profile"]] == ["Name: Absurd"]
    assert [t["text"] for t in h["today"]] == ["standup"]
    assert [t["text"] for t in h["open_tasks"]] == ["call mum"]
    assert vault.hot_size() < HOT_CACHE_MAX


def test_pause_and_export(vault, tmp_path, clock):
    vault.add("profile", "likes rust")
    vault.set_paused(True)
    assert vault.paused
    out = vault.export(tmp_path / "export.json")
    data = json.loads((tmp_path / "export.json").read_text())
    assert out["items"] == 1 and data["items"][0]["text"] == "likes rust"
    assert stat.S_IMODE(os.stat(tmp_path / "export.json").st_mode) == 0o600


# ---------- reminder daemon ----------

def make_daemon(vault, locked=lambda: False, jobs=lambda pid, unit: None, maintenance_done: bool = True):
    calls = {"notify": [], "speak": []}
    if maintenance_done:  # keep the day's maintenance out of reminder tests (tested on its own)
        vault.set_setting("last_maintenance", vault.clock().isoformat())
    d = Daemon(vault, lambda t, b: calls["notify"].append((t, b)) or (True, "sent"),
               lambda text: calls["speak"].append(text) or {"spoken": True}, persona=Persona(["Absurd"]),
               locked=locked, jobs=jobs)
    return d, calls


def test_reminder_fires_once_at_due_time(vault, clock):
    rid = vault.add("schedule", "check the training run", event_time=T0 + dt.timedelta(minutes=2),
                    meta={"type": "reminder"})
    d, calls = make_daemon(vault)
    clock.advance(seconds=90)
    assert d.tick() == [] and calls["notify"] == []
    clock.advance(seconds=30)                       # exactly due
    ev = d.tick()
    assert [e["event"] for e in ev] == ["reminder"]
    assert calls["notify"] == [("AIRA reminder", "check the training run")]
    assert len(calls["speak"]) == 1 and "check the training run" in calls["speak"][0] and "Absurd" in calls["speak"][0]
    assert vault.get(rid).meta["fired"] == "2026-11-14T10:02:00"
    clock.advance(seconds=30)
    assert d.tick() == []                           # not repeated


def test_reminder_repeats_at_unlock(vault, clock):
    vault.add("schedule", "stretch", event_time=T0 + dt.timedelta(minutes=1), meta={"type": "reminder"})
    state = {"locked": True}
    d, calls = make_daemon(vault, locked=lambda: state["locked"])
    clock.advance(minutes=1)
    d.tick()
    clock.advance(minutes=5)
    d.tick()
    assert len(calls["speak"]) == 1                 # still locked: no repeat yet
    state["locked"] = False
    clock.advance(seconds=30)
    ev = d.tick()
    assert [e["event"] for e in ev] == ["reminder_repeat"]
    assert calls["speak"][-1].startswith("While you were away") and "stretch" in calls["speak"][-1]
    clock.advance(seconds=30)
    assert d.tick() == []


def test_watched_job_and_briefing(vault, clock):
    vault.db.execute("INSERT INTO jobs(label, pid, started) VALUES ('training run', 4242, ?)", (T0.isoformat(),))
    vault.db.commit()
    vault.add("schedule", "group meeting", event_time=T0 + dt.timedelta(hours=4), meta={"type": "event"})
    vault.add("schedule", "submit form", event_time=T0 + dt.timedelta(hours=6), meta={"type": "reminder"})
    states = {"s": ("running", "pid 4242")}
    d, calls = make_daemon(vault, jobs=lambda pid, unit: states["s"])
    assert d.tick() == []
    states["s"] = ("failed", "exit status 1")
    clock.advance(seconds=30)
    ev = d.tick()
    assert ev[0]["event"] == "job" and ev[0]["state"] == "failed"
    assert "training run has failed" in calls["speak"][-1]
    b = briefing(vault, clock(), Persona(["Absurd"]))
    assert b["text"].startswith("Good morning, Absurd") and "group meeting at 14:00" in b["text"]
    assert "submit form at 16:00" in b["text"] and "Watched job training run failed." in b["text"]
    assert "Watched job" not in briefing(vault, clock(), Persona(["Absurd"]))["text"]  # only once


def test_nightly_maintenance_runs_once_after_3am(vault, clock):
    d, _ = make_daemon(vault, maintenance_done=False)
    clock.t = dt.datetime(2026, 11, 15, 2, 59)
    assert not [e for e in d.tick() if e["event"] == "maintenance"]
    clock.t = dt.datetime(2026, 11, 15, 3, 0)
    assert [e for e in d.tick() if e["event"] == "maintenance"]
    clock.advance(minutes=1)
    assert not [e for e in d.tick() if e["event"] == "maintenance"]
