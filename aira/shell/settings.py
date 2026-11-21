"""AIRA Settings: the state shown on the settings page, and the changes it may make.

Every change is read back before it is reported as saved, like AIRA's own actions (grounding
layer 6). Nothing here needs root, and nothing leaves the machine.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from .. import persona as persona_mod
from ..tts import FEMALE_VOICES

MiB = 1024 * 1024
RECOMMENDATION = Path("/var/lib/aira/recommendation.json")
NAME_RE = re.compile(r"^[\w .'-]{1,32}$", re.UNICODE)


class SettingsError(ValueError):
    pass


def _voice_name(voice_id: str) -> str:
    for name, vid in FEMALE_VOICES.items():
        if vid == voice_id:
            return name
    return voice_id


def state(orch, voice_session=None) -> dict:
    v = orch.vault
    counts = {}
    with v._lock:
        for kind, n in v.db.execute("SELECT kind, COUNT(*) FROM items GROUP BY kind"):
            counts[kind] = n
    current_voice = v.setting("voice") or (voice_session.speaker.voice if voice_session else "af_heart")
    actions = []
    for e in orch.action_log()[-15:][::-1]:
        actions.append({"when": dt.datetime.fromtimestamp(e.get("ts", 0)).strftime("%Y-%m-%d %H:%M"),
                        "agent": e.get("agent"), "tool": e.get("tool"), "allowed": e.get("allowed"),
                        "decision": e.get("decision"), "ok": e.get("ok")})
    rec = None
    if RECOMMENDATION.is_file():
        try:
            rec = json.loads(RECOMMENDATION.read_text())
        except (OSError, ValueError):
            rec = None
    return {
        "voice": {"current": _voice_name(current_voice),
                  "choices": [n.capitalize() for n in FEMALE_VOICES],
                  "piper_fallback": bool(voice_session and type(voice_session.speaker).__name__ == "PiperSpeaker")},
        "names": list(orch.persona.names),
        "memory": {"paused": v.paused, "quota_mb": round(v.quota / MiB), "used_mb": round(v.used_bytes() / MiB, 1),
                   "items": counts, "min_mb": 64, "max_mb": 2048},
        "model": {"server": orch.config.server_url, "recommendation": rec},
        "actions": actions,
        "phones": _phones(),
    }


def _phones() -> list[dict]:
    """Phones paired with AIROX Connect (read-only here; pairing and removal are in its window)."""
    try:
        from ..tools.phone import call
        return [{"name": p["name"], "online": p["online"], "battery": (p.get("status") or {}).get("battery")}
                for p in call("phone_status", timeout=3)["phones"]]
    except Exception:
        return []


def apply(orch, change: dict, voice_session=None) -> dict:
    """Apply one change; return what was verified. Raises SettingsError for a bad request."""
    what = change.get("what")
    v = orch.vault
    if what == "voice":
        name = str(change.get("value", "")).strip().lower()
        if name not in FEMALE_VOICES:
            raise SettingsError(f"unknown voice {name!r}")
        v.set_setting("voice", FEMALE_VOICES[name])
        if voice_session is not None and type(voice_session.speaker).__name__ != "PiperSpeaker":
            voice_session.speaker.voice = FEMALE_VOICES[name]
        ok = v.setting("voice") == FEMALE_VOICES[name]
        return {"ok": ok, "message": f"AIRA now speaks with the {name.capitalize()} voice." if ok
                else "I couldn't verify that the voice was changed."}
    if what == "names":
        raw = change.get("value", [])
        names = [str(n).strip() for n in (raw if isinstance(raw, list) else str(raw).split(",")) if str(n).strip()]
        if len(names) > 6 or not all(NAME_RE.match(n) for n in names):
            raise SettingsError("names must be up to 6 short names (letters, spaces, . ' -)")
        p = persona_mod.config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("names = [" + ", ".join(json.dumps(n) for n in names) + "]\n")
        orch.persona.names = persona_mod.load_names()
        ok = orch.persona.names == names or (not names and orch.persona.names == persona_mod.gecos_first_name())
        return {"ok": ok, "message": ("AIRA will call you " + ", ".join(names) + "." if names
                                      else "AIRA will use your account's first name.") if ok
                else "I couldn't verify the new names."}
    if what == "memory_paused":
        on = bool(change.get("value"))
        v.set_paused(on)
        ok = v.paused == on
        return {"ok": ok, "message": ("Memory is paused: AIRA won't record anything new." if on
                                      else "Memory is recording again.") if ok else "I couldn't verify that."}
    if what == "quota_mb":
        try:
            mb = int(change.get("value"))
        except (TypeError, ValueError):
            raise SettingsError("the quota must be a number of MB") from None
        try:
            v.set_quota(mb * MiB)
        except ValueError as exc:
            raise SettingsError(str(exc)) from None
        ok = v.quota == mb * MiB
        return {"ok": ok, "message": f"Memory quota set to {mb} MB." if ok else "I couldn't verify the quota."}
    if what == "export":
        dest = Path.home() / f"aira-memory-export-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        r = v.export(dest)
        ok = dest.is_file()
        return {"ok": ok, "message": f"Exported {r['items']} items to {dest}." if ok
                else "I couldn't verify the export file.", "file": str(dest)}
    raise SettingsError(f"unknown setting {what!r}")
