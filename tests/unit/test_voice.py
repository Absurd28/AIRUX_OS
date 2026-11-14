"""Voice loop tests: fake speech services, scripted model (no servers needed)."""
from __future__ import annotations

import datetime as dt
import http.server
import json
import threading
from pathlib import Path

import pytest

from aira.config import Config
from aira.memory.embed import HashEmbedder
from aira.memory.vault import Vault
from aira.orchestrator import Orchestrator
from aira.tts import PiperSpeaker, Speaker, choose_speaker
from aira.voice import DIDNT_CATCH, STTError, Transcriber, VoiceSession, VoiceTurn, spoken_text, turn_report
from test_guard import FakeLLM, call, reply, route


class FakeSTT:
    def __init__(self, text=None, error=None):
        self.text, self.error = text, error

    def transcribe(self, wav):
        if self.error:
            raise STTError(self.error)
        return self.text


class FakeSpeaker(Speaker):
    def __init__(self):
        super().__init__(play=False)
        self.said = []

    def say(self, text):
        self.said.append((self.voice, text))
        return {"spoken": True}


@pytest.fixture
def orch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "notes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    def build(script):
        cfg = Config(server_url="http://fake", data_dir=tmp_path / "data")
        vault = Vault(cfg.data_dir / "vault.db", "test-key", embedder=HashEmbedder(),
                      clock=lambda: dt.datetime(2026, 11, 14, 10, 0))
        return Orchestrator(cfg, lambda *a: False, llm=FakeLLM(script), vault=vault)
    return build


def test_spoken_text_drops_verified_lines():
    assert spoken_text("Created ~/a.txt.\nVerified: ~/a.txt contains the text.") == "Created ~/a.txt."
    assert spoken_text("I couldn't verify that: clipboard failed: x") == "That didn't work: x."


def test_voice_turn_runs_command_and_speaks(orch, tmp_path):
    o = orch([route("desktop", "list notes"), call("list_dir", path="~/notes"), reply("Your notes folder is empty.")])
    sp = FakeSpeaker()
    vt = VoiceSession(o, FakeSTT("What's in my notes folder?"), sp).handle_audio(tmp_path / "x.wav")
    assert vt.transcript == "What's in my notes folder?"
    assert vt.turn.runs[0].tool_calls[0].tool == "list_dir"
    assert sp.said == [("af_heart", "Your notes folder is empty.")]


@pytest.mark.parametrize("text", ["", "[BLANK_AUDIO]", "(wind blowing)", " ... "])
def test_non_speech_is_not_sent_to_the_model(orch, tmp_path, text):
    o = orch([])  # the model must not be called
    sp = FakeSpeaker()
    vt = VoiceSession(o, FakeSTT(text), sp).handle_audio(tmp_path / "x.wav")
    assert vt.turn is None and sp.said == [("af_heart", DIDNT_CATCH)]


def test_stt_failure_is_reported(orch, tmp_path):
    sp = FakeSpeaker()
    vt = VoiceSession(orch([]), FakeSTT(error="whisper-server down"), sp).handle_audio(tmp_path / "x.wav")
    assert sp.said[0][1].startswith("I couldn't verify that") and "whisper-server down" in sp.said[0][1]


def test_change_voice_by_voice(orch, tmp_path):
    o = orch([{"voice": "bella"}, {"chat": "thanks"}])
    sp = FakeSpeaker()
    s = VoiceSession(o, FakeSTT("Change your voice to Bella."), sp)
    s.handle_audio(tmp_path / "x.wav")
    assert o.vault.setting("voice") == "af_bella"
    assert sp.said[-1][0] == "af_bella" and "Bella voice" in sp.said[-1][1]
    s.stt = FakeSTT("Thanks")
    s.handle_audio(tmp_path / "y.wav")
    assert sp.said[-1][0] == "af_bella"
    # a new session picks the saved voice up
    sp2 = FakeSpeaker()
    VoiceSession(o, FakeSTT("x"), sp2)
    assert sp2.voice == "af_bella"


def test_voice_comes_from_users_words(orch, tmp_path):
    o = orch([{"voice": "heart"}, {"voice": "bella"}])  # model picks wrongly, then names nothing said
    sp = FakeSpeaker()
    s = VoiceSession(o, FakeSTT("Change your voice to Bella."), sp)
    s.handle_audio(tmp_path / "x.wav")
    assert o.vault.setting("voice") == "af_bella" and sp.said[-1][0] == "af_bella"
    s.stt = FakeSTT("Use a different voice please.")
    vt = s.handle_audio(tmp_path / "y.wav")
    assert vt.turn.kind == "ask" and vt.turn.reply.startswith("Which voice would you like")
    assert o.vault.setting("voice") == "af_bella"


def test_transcriber_sends_vocabulary_prompt():
    from aira.voice import VOCABULARY
    assert "Neovim" in VOCABULARY and "AIRA" in VOCABULARY


def test_voice_enum_rejects_unknown_names(orch):
    o = orch([])
    voices = [opt for opt in o.router_schema["oneOf"] if "voice" in opt["properties"]][0]
    assert "bella" in voices["properties"]["voice"]["enum"] and "michael" not in voices["properties"]["voice"]["enum"]


def test_choose_speaker_fallback():
    assert isinstance(choose_speaker(5 * 1024 ** 3), PiperSpeaker)
    s = choose_speaker(14 * 1024 ** 3)
    assert type(s) is Speaker and s.voice == "af_heart"


def test_transcriber_multipart_roundtrip(tmp_path):
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen["ctype"] = self.headers["Content-Type"]
            seen["body"] = body
            out = json.dumps({"text": " hello there "}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFFxxxxWAVE")
    try:
        assert Transcriber(f"http://127.0.0.1:{srv.server_port}").transcribe(wav) == "hello there"
    finally:
        srv.shutdown()
    assert seen["ctype"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="a.wav"' in seen["body"] and b"RIFFxxxxWAVE" in seen["body"]
    assert b'name="response_format"\r\n\r\njson' in seen["body"]


@pytest.mark.skipif(not (Path("/srv/airux-work/models/kokoro-v1.0.onnx")).exists(), reason="Kokoro not downloaded")
def test_kokoro_runs_out_of_process(tmp_path):
    import subprocess
    import sys
    sp = Speaker(play=False)
    try:
        secs = sp.synth("Hello Aryan.", tmp_path / "a.wav")
        assert secs > 0.5 and (tmp_path / "a.wav").stat().st_size > 10_000
        assert "af_bella" in sp.voices()
        assert "kokoro_onnx" not in sys.modules and "phonemizer" not in sys.modules
        assert sp._worker.poll() is None             # one long-lived worker
        sp.synth("Again.", tmp_path / "b.wav")
    finally:
        sp.close()
    # every module in this process stays free of the GPL speech stack
    out = subprocess.run([sys.executable, "-c", "import aira.voice, aira.tts, aira.orchestrator, sys; "
                          "print(any(m.startswith(('kokoro_onnx','phonemizer','espeakng')) for m in sys.modules))"],
                         capture_output=True, text=True)
    assert out.stdout.strip() == "False"


def test_transcriber_unreachable():
    with pytest.raises(STTError):
        Transcriber("http://127.0.0.1:9", timeout=2).transcribe(Path(__file__))


def test_playback_failure_is_reported_not_hidden():
    vt = VoiceTurn("a.wav", "hello", spoken="Hi boss.",
                   speech={"spoken": False, "error": "Connection failure: Connection refused"})
    assert "not spoken aloud: Connection failure" in turn_report(vt)
    ok = VoiceTurn("a.wav", "hello", spoken="Hi boss.", speech={"spoken": True})
    assert "not spoken" not in turn_report(ok)
    off = VoiceTurn("a.wav", "hello", spoken="Hi boss.", speech={"spoken": False, "error": "playback disabled"})
    assert "not spoken" not in turn_report(off)
