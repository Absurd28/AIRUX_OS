"""AIRA's voice: Kokoro-82M (ONNX), voice af_heart by default, played with paplay.

The model loads once (about 1.5 s) and synthesis runs on the CPU. Each utterance is also written
as a WAV under the data folder so it can be checked. Phase 5 adds the Piper fallback for machines
with under 6 GB of memory and voice switching.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

MODELS = Path(os.environ.get("AIRUX_WORK", "/srv/airux-work")) / "models"
DEFAULT_VOICE = "af_heart"
# female Kokoro presets AIRA may switch to by voice ("change your voice to Bella")
FEMALE_VOICES = {"heart": "af_heart", "bella": "af_bella", "nicole": "af_nicole", "sarah": "af_sarah",
                 "sky": "af_sky", "nova": "af_nova", "emma": "bf_emma", "isabella": "bf_isabella"}
PIPER_VOICE = "en_US-ljspeech-medium"
FALLBACK_BELOW_BYTES = 6 * 1024 ** 3   # CLAUDE.md: Piper female voice under 6 GB of memory


class Speaker:
    def __init__(self, voice: str = DEFAULT_VOICE, out_dir: Path | None = None, play: bool = True):
        self.voice = voice
        self.out_dir = out_dir
        self.play = play
        self._kokoro = None
        self._lock = threading.Lock()

    def _engine(self):
        if self._kokoro is None:
            from kokoro_onnx import Kokoro
            self._kokoro = Kokoro(str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin"))
        return self._kokoro

    def voices(self) -> list[str]:
        return list(self._engine().get_voices())

    def synth(self, text: str, path: Path) -> float:
        """Write speech for `text` to a WAV file; returns its duration in seconds."""
        import numpy as np
        samples, sr = self._engine().create(text, voice=self.voice, speed=1.0, lang="en-us")
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        return len(samples) / sr

    def say(self, text: str) -> dict:
        """Speak text. Returns what happened, never raises (a reminder must not die on audio)."""
        with self._lock:
            out = (self.out_dir or Path("/tmp")) / f"aira-say-{int(time.time() * 1000)}.wav"
            try:
                seconds = self.synth(text, out)
            except Exception as exc:
                return {"spoken": False, "error": f"speech synthesis failed: {exc}"}
            if not self.play:
                return {"spoken": False, "wav": str(out), "seconds": round(seconds, 2), "error": "playback disabled"}
            exe = shutil.which("paplay")
            if not exe:
                return {"spoken": False, "wav": str(out), "error": "paplay is not installed (pulseaudio-utils)"}
            try:
                r = subprocess.run([exe, str(out)], capture_output=True, text=True, timeout=seconds + 15)
            except subprocess.TimeoutExpired:
                return {"spoken": False, "wav": str(out), "error": "audio playback timed out"}
            if r.returncode != 0:
                return {"spoken": False, "wav": str(out), "error": r.stderr.strip() or f"paplay exit {r.returncode}"}
            return {"spoken": True, "wav": str(out), "seconds": round(seconds, 2)}


class PiperSpeaker(Speaker):
    """Fallback voice for machines under 6 GB: Piper (GPL-3.0) run as a separate program,
    voice en_US-ljspeech-medium (public-domain LJ Speech dataset)."""

    def __init__(self, out_dir: Path | None = None, play: bool = True):
        super().__init__(voice=PIPER_VOICE, out_dir=out_dir, play=play)

    def voices(self) -> list[str]:
        return [PIPER_VOICE]

    def synth(self, text: str, path: Path) -> float:
        import sys
        path.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([sys.executable, "-m", "piper", "-m", str(MODELS / f"{PIPER_VOICE}.onnx"), "-f", str(path)],
                           input=text, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-300:] or f"piper exit {r.returncode}")
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()


def choose_speaker(total_memory: int, voice: str = DEFAULT_VOICE, out_dir: Path | None = None,
                   play: bool = True) -> Speaker:
    """Kokoro normally; Piper under 6 GB of total memory (CLAUDE.md)."""
    if total_memory < FALLBACK_BELOW_BYTES:
        return PiperSpeaker(out_dir=out_dir, play=play)
    return Speaker(voice=voice, out_dir=out_dir, play=play)
