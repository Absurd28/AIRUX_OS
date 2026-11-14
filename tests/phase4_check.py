#!/usr/bin/env python3
"""Phase 4 check: memory vault and reminders.

A. A 2-minute reminder set by voice-command text through AIRA fires by voice and notification:
   the reminder daemon runs as a systemd user service (systemd-run --user), the Notify call is
   captured on the session D-Bus, the spoken WAV is checked (length, not silent) and played with
   paplay, and it fires within 30 s of the due time.
B. With a fake clock, expiry, compaction and quota work (the vault unit tests are run here).
C. "forget X" removes X from both keyword and vector search (real nomic embeddings), and leaves
   unrelated memories.
Run as the desktop user (needs the session bus and audio), from the repo, with the test venv.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from fixture import build_fixture, start_server  # noqa: E402

FIXTURE = Path(tempfile.gettempdir()) / "aira-p4"
EMBED_PORT = 18081
UNIT = f"aira-reminderd-p4-{os.getpid()}"


def wav_stats(path: Path) -> tuple[float, float]:
    import array
    with wave.open(str(path)) as w:
        frames = w.readframes(w.getnframes())
        secs = w.getnframes() / w.getframerate()
    a = array.array("h", frames)
    peak = max((abs(x) for x in a), default=0) / 32767
    return secs, peak


def part_a(orch, cfg, env) -> bool:
    print("\n=== A. 2-minute reminder by voice and notification", flush=True)
    capture = FIXTURE / "dbus-notify.txt"
    mon = subprocess.Popen(["dbus-monitor", "--session",
                            "interface='org.freedesktop.Notifications',member='Notify'"],
                           stdout=capture.open("w"), stderr=subprocess.STDOUT)
    unit_env = []
    for k in ("HOME", "AIRA_DATA", "XDG_CONFIG_HOME", "AIRA_EMBED_SERVER", "DISPLAY", "WAYLAND_DISPLAY",
              "PULSE_SERVER", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "AIRUX_WORK"):
        if env.get(k):
            unit_env += ["--setenv", f"{k}={env[k]}"]
    r = subprocess.run(["systemd-run", "--user", "--unit", UNIT, "--working-directory", str(REPO), *unit_env,
                        sys.executable, "-m", "aira.reminderd"], capture_output=True, text=True)
    print(f"systemd-run: exit {r.returncode} {r.stderr.strip()}")
    if r.returncode != 0:
        mon.terminate()
        return False
    try:
        time.sleep(3)
        state = subprocess.run(["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True).stdout.strip()
        print(f"reminder daemon unit {UNIT}: {state}")
        t_cmd = dt.datetime.now()
        turn = orch.handle("Remind me in 2 minutes to check the training run.")
        calls = [(s.tool, s.args, s.verified) for run in turn.runs for s in run.tool_calls]
        print(f"AIRA: {turn.reply!r}\n  calls: {calls}")
        rem = [it for it in orch.vault.open_reminders(dt.datetime.now()) if "training run" in it.text]
        if not rem:
            print("FAIL no reminder stored in the vault")
            return False
        due = dt.datetime.fromisoformat(rem[0].event_time)
        print(f"stored reminder id {rem[0].id} due {due} (command at {t_cmd:%H:%M:%S})")
        log = cfg.data_dir / "reminderd.log"
        fired = None
        deadline = due + dt.timedelta(seconds=75)
        while dt.datetime.now() < deadline and fired is None:
            time.sleep(5)
            if log.exists():
                for line in log.read_text().splitlines():
                    e = json.loads(line)
                    if e.get("event") == "reminder" and e.get("id") == rem[0].id:
                        fired = e
        if fired is None:
            print(f"FAIL reminder did not fire by {deadline:%H:%M:%S}")
            return False
        fired_at = dt.datetime.fromisoformat(fired["ts"])
        late = (fired_at - due).total_seconds()
        print(f"fired at {fired_at:%H:%M:%S} ({late:.0f}s after due); notified={fired['notified']} "
              f"spoken={fired.get('spoken')} wav={fired.get('wav')} error={fired.get('error')}")
        time.sleep(1)
        mon.terminate()
        cap = capture.read_text(errors="replace")
        on_bus = 'string "AIRA reminder"' in cap and "check the training run" in cap
        secs, peak = wav_stats(Path(fired["wav"])) if fired.get("wav") else (0, 0)
        print(f"notification seen on D-Bus: {on_bus}; speech WAV {secs:.2f}s, peak {peak:.2f}")
        ok = fired["notified"] and on_bus and fired.get("spoken") is True and secs > 1.0 and peak > 0.05 \
            and 0 <= late <= 30
        print("PASS" if ok else "FAIL")
        return ok
    finally:
        mon.terminate()
        subprocess.run(["systemctl", "--user", "stop", UNIT], capture_output=True)


def part_b() -> bool:
    print("\n=== B. fake clock: expiry, compaction, decay, quota (vault tests)", flush=True)
    tests = ["test_retention_rules", "test_decay_and_boost", "test_quota_evicts_lowest_scored_episodes_only",
             "test_hot_cache", "test_vault_is_encrypted", "test_nightly_maintenance_runs_once_after_3am"]
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/unit/test_memory.py",
                        "-k", " or ".join(tests)], cwd=REPO, capture_output=True, text=True)
    print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-500:])
    print("PASS" if r.returncode == 0 else "FAIL")
    return r.returncode == 0


def part_c(orch) -> bool:
    print("\n=== C. forget X removes X from both searches (real embeddings)", flush=True)
    for cmd in ["Remember that project Falcon uses PyTorch 2.5 and trains on 8 GPUs.",
                "Remember that I prefer dark mode in all my editors."]:
        t = orch.handle(cmd)
        print(f"> {cmd}\nAIRA: {t.reply!r}")
    v = orch.vault
    before = {"keyword": [i.text for i in v.keyword_search("falcon")],
              "vector": [(i.text, round(d, 2)) for i, d in v.vector_search("project falcon", k=3)]}
    print(f"before forget: {before}")
    t = orch.handle("What do you know about me?")
    print(f"> What do you know about me?\nAIRA: {t.reply!r}")
    t = orch.handle("Forget everything about project Falcon.")
    print(f"> Forget everything about project Falcon.\nAIRA: {t.reply!r}")
    events = [e for r in t.runs for e in r.guard_events]
    for e in events:
        print(f"  guard: {e[:160]}")
    left = v.topic_present("project falcon")
    kw = v.keyword_search("falcon")
    vec = [(i.text, round(d, 2)) for i, d in v.vector_search("project falcon", k=3)]
    profile = [i.text for i in v.by_kind("profile")]
    actions = orch.action_log()
    print(f"after: topic_present={left}; keyword('falcon') over all kinds incl. action log="
          f"{[i.text[:120] for i in kw]}; nearest vectors={vec}")
    print(f"profile now: {profile}")
    print(f"action log still records the calls: {[(a['tool'], a['args']) for a in actions[-3:]]}")
    # no plain-text copy anywhere in AIRA's data folder (the vault itself is encrypted)
    plain = [str(p) for p in Path(os.environ["AIRA_DATA"]).rglob("*")
             if p.is_file() and b"falcon" in p.read_bytes().lower()]
    print(f"files in the data folder containing 'falcon' in plain text: {plain}")
    ok = bool(before["keyword"]) and not left["keyword"] and not left["vector"] and not kw and not plain \
        and any("dark mode" in p for p in profile) and not any("falcon" in p.lower() for p in profile) \
        and any(a["tool"] == "forget" for a in actions)
    print("PASS" if ok else "FAIL")
    return ok


def main() -> int:
    home = build_fixture(FIXTURE)
    env = os.environ
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["AIRA_DATA"] = str(FIXTURE / "aira-data")
    env["AIRA_EMBED_SERVER"] = f"http://127.0.0.1:{EMBED_PORT}"
    env.pop("XDG_DATA_HOME", None)

    from aira.config import Config
    from aira.orchestrator import Orchestrator
    from aira.profiler import launch

    embed = launch.start_embedder(EMBED_PORT, Path(tempfile.gettempdir()) / "aira-p4-embed.log")
    server, note = (None, env["AIRA_SERVER"]) if "AIRA_SERVER" in env else start_server(18800, "aira-p4-server.log")
    asked = []

    def confirm(agent, tool, args, risks):  # approve only the forget this check asks for
        asked.append(tool)
        return tool == "forget"

    try:
        cfg = Config()
        orch = Orchestrator(cfg, confirm)
        key_file = home / ".config" / "aira" / "vault.key"
        print(f"vault {cfg.vault_path}; key from {'file ' + str(key_file) if key_file.exists() else 'keyring'}; "
              f"model {note}")
        results = {"A_reminder": part_a(orch, cfg, env), "B_fake_clock": part_b(), "C_forget": part_c(orch)}
    finally:
        launch.stop(embed)
        if server is not None:
            launch.stop(server)
    print(f"\nconfirmation requests: {asked}")
    print("=== phase 4:", "PASS" if all(results.values()) else "FAIL", results)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
