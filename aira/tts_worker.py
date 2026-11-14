"""Kokoro speech synthesis in its own process: python -m aira.tts_worker

Kokoro's phonemes come from eSpeak NG and phonemizer, which are GPL-3.0. Running synthesis in a
separate program (like Piper) keeps them out of AIRA's own process. Protocol: one JSON object per
line on stdin {"text", "voice", "path"}; one JSON line back {"ok": true, "seconds": x} or
{"ok": false, "error": "..."}. The model loads once and stays loaded.
"""
from __future__ import annotations

import json
import os
import sys
import wave
from pathlib import Path

MODELS = Path(os.environ.get("AIRA_MODELS", Path(os.environ.get("AIRUX_WORK", "/srv/airux-work")) / "models"))


def main() -> int:
    import numpy as np
    from kokoro_onnx import Kokoro
    k = Kokoro(str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin"))
    out = sys.stdout
    out.write(json.dumps({"ok": True, "ready": True, "voices": sorted(k.get_voices())}) + "\n")
    out.flush()
    for line in sys.stdin:
        try:
            req = json.loads(line)
            samples, sr = k.create(req["text"], voice=req.get("voice", "af_heart"), speed=float(req.get("speed", 1.0)),
                                   lang="en-us")
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
            path = Path(req["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(pcm.tobytes())
            reply = {"ok": True, "seconds": round(len(samples) / sr, 3)}
        except Exception as exc:  # report, keep serving
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        out.write(json.dumps(reply) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
