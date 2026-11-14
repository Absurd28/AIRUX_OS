#!/usr/bin/env python3
"""Phase 5 check: ten spoken commands work with the network off.

Run inside a network namespace with only loopback:
    unshare -rn bash -c 'ip link set lo up; python tests/phase5_check.py'
The check first proves there is no network. It then starts llama-server, the embedding server
and whisper-server inside the namespace, speaks ten commands with a Kokoro voice that is not
AIRA's (am_michael, standing in for the user), and runs each through the real voice loop:
whisper.cpp -> AIRA -> Kokoro reply played with paplay. A command works when the transcript is
close to what was said (word error rate <= 0.25), AIRA makes the expected call, and the reply is
spoken. The Piper fallback voice is exercised once at the end.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from fixture import build_fixture, start_server  # noqa: E402

FIXTURE = Path(tempfile.gettempdir()) / "aira-p5"
WORK = Path(os.environ.get("AIRUX_WORK", "/srv/airux-work"))
EMBED_PORT, STT_PORT = 18081, 18082

COMMANDS = [
    ("Hello AIRA, who are you?", {"chat": None}),
    ("How much free disk space do I have in my home folder?", {"tool": "disk_usage"}),
    ("What GPU do I have and how hot is it?", {"tool": "gpu_status"}),
    ("What's in my notes folder?", {"tool": "list_dir"}),
    ("Search my notes for transformer divergence.", {"tool": "search_notes"}),
    ("What's the git status of the demo project in my projects folder?", {"tool": "git_status"}),
    ("Remind me in ten minutes to stretch.", {"tool": "add_reminder"}),
    ("Remember that my favourite editor is Neovim.", {"tool": "remember"}),
    ("What do you know about me?", {"tool": "recall"}),
    ("Change your voice to Bella.", {"voice": "af_bella"}),
]
NUMBERS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
           "eight": "8", "nine": "9", "ten": "10"}
# the same word in British and American spelling is not a recognition error
SPELLING = {"favourite": "favorite", "colour": "color", "organise": "organize", "summarise": "summarize",
            "centre": "center", "analyse": "analyze"}


def words(s: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9']+", s.lower().replace("’", "'")):
        w = NUMBERS.get(w, w)
        out.append(SPELLING.get(w, w))
    return out


def wer(ref: str, hyp: str) -> float:
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)] / max(1, len(r))


def network_is_off() -> bool:
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            socket.create_connection((host, port), timeout=3).close()
            return False
        except OSError:
            pass
    try:
        socket.getaddrinfo("huggingface.co", 443)
        return False
    except OSError:
        pass
    routes = subprocess.run(["ip", "route"], capture_output=True, text=True).stdout.strip()
    print(f"network check: 1.1.1.1 and 8.8.8.8 unreachable, DNS fails, routes: {routes or '(none)'}")
    return True


def start_whisper(model: str = "ggml-small.en.bin") -> subprocess.Popen:
    b = WORK / "bin-whisper"
    log = (Path(tempfile.gettempdir()) / "aira-p5-whisper.log").open("w")
    p = subprocess.Popen([str(b / "whisper-server"), "-m", str(WORK / "models" / model), "--host", "127.0.0.1",
                          "--port", str(STT_PORT), "-t", "6"], env=dict(os.environ, LD_LIBRARY_PATH=str(b)),
                         stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{STT_PORT}/", timeout=2)
            return p
        except Exception:
            time.sleep(0.5)
    p.kill()
    raise RuntimeError("whisper-server did not start")


def speak_as_user(text: str, path: Path, kokoro) -> None:
    import numpy as np
    s, sr = kokoro.create(text, voice="am_michael", speed=1.0, lang="en-us")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(s, -1, 1) * 32767).astype("<i2").tobytes())


def judge(expect: dict, vt, orch) -> list[str]:
    t = vt.turn
    if t is None:
        return ["no turn (not understood)"]
    if "chat" in expect:
        return [] if t.kind == "reply" and "Adaptive Intelligent Research Assistant" in t.reply else [f"kind {t.kind}"]
    if "voice" in expect:
        return [] if t.kind == "voice" and orch.vault.setting("voice") == expect["voice"] else [f"voice not set ({t.kind})"]
    calls = [s.tool for r in t.runs for s in r.tool_calls]
    return [] if calls and calls[0] == expect["tool"] else [f"calls {calls or t.kind}"]


def main() -> int:
    if not network_is_off():
        print("FAIL: the network is reachable; run inside `unshare -rn`")
        return 1
    home = build_fixture(FIXTURE)
    env = os.environ
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), AIRA_DATA=str(FIXTURE / "aira-data"),
               AIRA_EMBED_SERVER=f"http://127.0.0.1:{EMBED_PORT}", AIRA_STT_SERVER=f"http://127.0.0.1:{STT_PORT}")
    env.pop("XDG_DATA_HOME", None)

    from kokoro_onnx import Kokoro

    from aira.config import Config
    from aira.orchestrator import Orchestrator
    from aira.profiler import launch
    from aira.tts import PiperSpeaker, Speaker
    from aira.voice import Transcriber, VoiceSession

    procs = [launch.start_embedder(EMBED_PORT, Path(tempfile.gettempdir()) / "aira-p5-embed.log"), start_whisper()]
    server, note = start_server(18900, "aira-p5-server.log")
    procs.append(server)
    results = []
    try:
        cfg = Config()
        orch = Orchestrator(cfg, lambda *a: False)
        speaker = Speaker(out_dir=cfg.data_dir / "speech", play=True)
        session = VoiceSession(orch, Transcriber(), speaker)
        user_voice = Kokoro(str(WORK / "models/kokoro-v1.0.onnx"), str(WORK / "models/voices-v1.0.bin"))
        audio = FIXTURE / "spoken"
        audio.mkdir(parents=True, exist_ok=True)
        for i, (said, expect) in enumerate(COMMANDS, 1):
            wav = audio / f"cmd{i:02}.wav"
            speak_as_user(said, wav, user_voice)
            t0 = time.monotonic()
            vt = session.handle_audio(wav)
            total = time.monotonic() - t0
            e = wer(said, vt.transcript)
            problems = [] if e <= 0.25 else [f"WER {e:.2f}"]
            problems += judge(expect, vt, orch)
            if not vt.speech.get("spoken"):
                problems.append(f"reply not spoken: {vt.speech.get('error')}")
            ok = not problems
            results.append(ok)
            print(f"{i:2}. {'OK  ' if ok else 'FAIL'} said: {said!r}\n    heard: {vt.transcript!r} (WER {e:.2f})"
                  f"\n    AIRA ({speaker.voice}): {vt.spoken[:200]!r}\n    stt {vt.stt_seconds:.1f}s, think "
                  f"{vt.think_seconds:.1f}s, speak {vt.speak_seconds:.1f}s, total {total:.1f}s"
                  + ("" if ok else f"\n    problems: {problems}"), flush=True)
        # Piper fallback (used under 6 GB of memory)
        piper = PiperSpeaker(out_dir=cfg.data_dir / "speech", play=True)
        pr = piper.say("This is AIRA's fallback voice for machines with less memory.")
        print(f"\nPiper fallback: {pr}")
        piper_ok = pr.get("spoken") is True
    finally:
        for p in procs:
            launch.stop(p)
    n = sum(results)
    print(f"\nspoken commands working offline: {n}/{len(COMMANDS)}; model {note}; Piper fallback ok: {piper_ok}")
    passed = n == len(COMMANDS) and piper_ok
    print("=== phase 5:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
