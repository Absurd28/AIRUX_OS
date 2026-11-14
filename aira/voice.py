"""Voice loop: push-to-talk -> whisper.cpp -> AIRA -> spoken reply.

    python -m aira.voice             push-to-talk in the terminal (Enter starts, Enter stops)
    python -m aira.voice --file x.wav   one command from a WAV file

The "Hey AIRA" wake word (openWakeWord) is not trained yet; CLAUDE.md allows push-to-talk until
it is. Phase 7 binds push-to-talk to a key in the AIRA shell.
Everything runs locally: whisper-server, llama-server and the voices need no network.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .tts import DEFAULT_VOICE, FEMALE_VOICES, PiperSpeaker, Speaker, choose_speaker

DIDNT_CATCH = "Sorry, I didn't catch that."
# Words AIRUX users say that a general speech model mishears; passed to whisper as its prompt.
VOCABULARY = ("AIRA, AIRUX, Neovim, Vim, VS Code, PyTorch, TensorFlow, NumPy, pandas, Jupyter, Podman, "
              "Docker, Postgres, arXiv, LaTeX, git, GPU, CUDA, systemd, Debian, Xfce, venv, pytest.")
# whisper's markers for non-speech, and very short noise
NON_SPEECH = re.compile(r"^\s*(\[[^\]]*\]|\([^)]*\)|\.+)?\s*$")


class STTError(RuntimeError):
    pass


class Transcriber:
    """Client for whisper.cpp's whisper-server (/inference), stdlib multipart upload."""

    def __init__(self, url: str | None = None, timeout: float = 60):
        self.url = (url or os.environ.get("AIRA_STT_SERVER", "http://127.0.0.1:8082")).rstrip("/")
        self.timeout = timeout

    def transcribe(self, wav: Path) -> str:
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (("response_format", "json"), ("temperature", "0"), ("language", "en"),
                            ("prompt", VOCABULARY)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{wav.name}"\r\n'
                     f"Content-Type: audio/wav\r\n\r\n".encode() + wav.read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(self.url + "/inference", data=b"".join(parts),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read()).get("text", "").strip()
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
            raise STTError(f"speech recognition unavailable at {self.url}: {exc}") from exc


class Recorder:
    """Records the default microphone with parecord (PulseAudio/PipeWire) until stopped."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.proc: subprocess.Popen | None = None
        self.path: Path | None = None

    def start(self) -> Path:
        exe = shutil.which("parecord")
        if not exe:
            raise STTError("parecord is not installed (package pulseaudio-utils)")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.out_dir / f"ptt-{int(time.time() * 1000)}.wav"
        self.proc = subprocess.Popen([exe, "--channels=1", "--rate=16000", "--format=s16le", "--file-format=wav",
                                      str(self.path)], stderr=subprocess.PIPE)
        return self.path

    def stop(self) -> Path:
        if self.proc is None or self.path is None:
            raise STTError("not recording")
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        return self.path


def spoken_text(reply: str) -> str:
    """What AIRA says aloud: the reply without code-written 'Verified:' lines (shown, not spoken)."""
    lines = [l for l in reply.splitlines() if l.strip() and not l.startswith("Verified:")]
    return " ".join(lines)


@dataclass
class VoiceTurn:
    audio: str
    transcript: str
    turn: object | None = None
    spoken: str = ""
    speech: dict = field(default_factory=dict)
    stt_seconds: float = 0.0
    think_seconds: float = 0.0
    speak_seconds: float = 0.0


class VoiceSession:
    def __init__(self, orchestrator, transcriber: Transcriber, speaker: Speaker):
        self.orch = orchestrator
        self.stt = transcriber
        self.speaker = speaker
        saved = orchestrator.vault.setting("voice")
        if saved and saved in FEMALE_VOICES.values() and not isinstance(speaker, PiperSpeaker):
            speaker.voice = saved

    def handle_audio(self, wav: Path) -> VoiceTurn:
        t0 = time.monotonic()
        try:
            text = self.stt.transcribe(wav)
        except STTError as exc:
            vt = VoiceTurn(str(wav), "", spoken=f"I couldn't verify that: {exc}")
            vt.speech = self.speaker.say(vt.spoken)
            return vt
        vt = VoiceTurn(str(wav), text, stt_seconds=time.monotonic() - t0)
        if NON_SPEECH.match(text) or len(re.findall(r"\w+", text)) == 0:
            vt.spoken = DIDNT_CATCH
        else:
            t1 = time.monotonic()
            vt.turn = self.orch.handle(text)
            vt.think_seconds = time.monotonic() - t1
            if getattr(vt.turn, "kind", "") == "voice" and not isinstance(self.speaker, PiperSpeaker):
                self.speaker.voice = self.orch.vault.setting("voice", DEFAULT_VOICE)
            vt.spoken = spoken_text(vt.turn.reply)
        t2 = time.monotonic()
        vt.speech = self.speaker.say(vt.spoken)
        vt.speak_seconds = time.monotonic() - t2
        return vt


def main(argv: list[str] | None = None) -> int:
    from .cli import tty_confirm
    from .config import Config
    from .orchestrator import Orchestrator
    from .profiler.probe import probe
    ap = argparse.ArgumentParser(prog="aira.voice")
    ap.add_argument("--file", type=Path, help="transcribe and run one WAV file")
    args = ap.parse_args(argv)
    cfg = Config()
    orch = Orchestrator(cfg, tty_confirm)
    speaker = choose_speaker(probe().total_memory, out_dir=cfg.data_dir / "speech")
    session = VoiceSession(orch, Transcriber(), speaker)
    if args.file:
        vt = session.handle_audio(args.file)
        print(f"you: {vt.transcript}\nAIRA: {vt.turn.reply if vt.turn else vt.spoken}")
        return 0
    rec = Recorder(cfg.data_dir / "recordings")
    print("AIRA voice. Press Enter to talk, Enter again to stop. Ctrl-C to quit.")
    while True:
        try:
            input()
            rec.start()
            print("listening...", flush=True)
            input()
            wav = rec.stop()
        except (EOFError, KeyboardInterrupt):
            return 0
        vt = session.handle_audio(wav)
        print(f"you: {vt.transcript}\nAIRA: {vt.turn.reply if vt.turn else vt.spoken}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
