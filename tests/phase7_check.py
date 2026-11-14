#!/usr/bin/env python3
"""Phase 7 check: "Open the browser and search for X" works by voice, through the AIRA shell.

The shell runs as in the OS: server + full-screen Chromium page (headed under WSLg). A spoken
command (Kokoro am_michael standing in for the user) goes through the push-to-talk path
(Shell.run_audio -> whisper -> AIRA). The browser confirmation is answered by clicking "Yes" in the
shell UI. Passes when the transcript matches, AIRA's browser search is verified by read-back and
independently by reading the open page, and the reply is spoken. Screenshots are saved.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from fixture import build_fixture, start_server  # noqa: E402
from phase5_check import EMBED_PORT, STT_PORT, speak_as_user, start_whisper, wer  # noqa: E402

FIXTURE = Path(tempfile.gettempdir()) / "aira-p7"
SAID = "Open the browser and search for block sparse attention."
QUERY_WORDS = ["block", "sparse", "attention"]


def main() -> int:
    home = build_fixture(FIXTURE)
    env = os.environ
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), XDG_STATE_HOME=str(home / ".local/state"),
               AIRA_DATA=str(FIXTURE / "aira-data"), AIRA_EMBED_SERVER=f"http://127.0.0.1:{EMBED_PORT}",
               AIRA_STT_SERVER=f"http://127.0.0.1:{STT_PORT}")
    env.pop("XDG_DATA_HOME", None)
    from kokoro_onnx import Kokoro
    from playwright.sync_api import sync_playwright

    from aira.config import Config
    from aira.orchestrator import Orchestrator
    from aira.profiler import launch
    from aira.shell.__main__ import token_file
    from aira.shell.server import Shell
    from aira.tts import Speaker
    from aira.voice import Transcriber, VoiceSession

    WORK = launch.WORK
    procs = [launch.start_embedder(EMBED_PORT, Path(tempfile.gettempdir()) / "aira-p7-embed.log"), start_whisper()]
    server, note = start_server(19100, "aira-p7-server.log")
    procs.append(server)
    shots = REPO / "docs" / "screenshots"   # persistent: WSL clears /tmp when the distro stops
    shots.mkdir(parents=True, exist_ok=True)
    ok = False
    orch = None
    try:
        cfg = Config()
        shell = Shell(port=0, token_file=token_file())
        orch = Orchestrator(cfg, shell.confirm, start_controller=True)
        speaker = Speaker(out_dir=cfg.data_dir / "speech", play=True)
        shell.attach(orch, VoiceSession(orch, Transcriber(), speaker))
        shell.serve()
        url = f"http://127.0.0.1:{shell.port}/"
        print(f"model {note}; shell on {url}; display={env.get('DISPLAY')}", flush=True)
        events = shell.subscribe()

        wav = FIXTURE / "said.wav"
        speak_as_user(SAID, wav, Kokoro(str(WORK / "models/kokoro-v1.0.onnx"), str(WORK / "models/voices-v1.0.bin")))

        with sync_playwright() as p:
            ui_browser = p.chromium.launch(executable_path="/usr/bin/chromium", headless=False,
                                           args=["--start-fullscreen", "--no-first-run"])
            ui = ui_browser.new_page(viewport={"width": 1600, "height": 900})
            ui.goto(url)
            ui.wait_for_selector("#agents .agent")
            t0 = time.monotonic()
            shell.run_audio(wav)                       # the push-to-talk path after recording stops
            clicked = []
            deadline = time.monotonic() + 420
            while time.monotonic() < deadline:
                card = ui.query_selector(".confirm")
                if card is not None:
                    text = card.inner_text()
                    choice = ".yes" if "wants to run browser" in text else ".no"
                    clicked.append((text.splitlines()[0], choice))
                    print(f"shell asks: {text.splitlines()[0]!r} -> clicking {choice}", flush=True)
                    ui.click(f".confirm {choice}")
                    ui.wait_for_timeout(500)
                if ui.query_selector(".msg.aira"):
                    break
                ui.wait_for_timeout(300)
            total = time.monotonic() - t0
            ui.wait_for_timeout(1500)
            ui.screenshot(path=str(shots / "phase7-shell.png"))
            heard = ui.inner_text(".msg.you").split("\n", 1)[-1].strip() if ui.query_selector(".msg.you") else ""
            reply = ui.inner_text(".msg.aira") if ui.query_selector(".msg.aira") else ""
            tags = [t.inner_text() for t in ui.query_selector_all(".msg.aira .tag")]
            ui_browser.close()

        evs = []
        while not events.empty():
            evs.append(events.get())
        aira_ev = next((e for e in evs if e.get("event") == "aira"), {})
        spoken = next((e for e in evs if e.get("event") == "spoken"), {})
        calls = aira_ev.get("calls", [])
        search = next((c for c in calls if c["tool"] == "browser" and c["args"].get("action") == "search"), None)
        page = orch.browser.read()
        orch.browser._call(lambda pg: pg.screenshot(path=str(shots / "phase7-aira-browser.png")))
        e = wer(SAID, heard)
        words_ok = search is not None and all(w in search["args"].get("query", "").lower() for w in QUERY_WORDS)
        page_ok = all(w in page["url"].lower() for w in QUERY_WORDS) and page.get("outbound_links", 0) >= 3
        print(f"heard: {heard!r} (WER {e:.2f})")
        print(f"calls: {calls}")
        print(f"AIRA (shell): {reply!r}\ntags: {tags}")
        print(f"spoken reply: {spoken.get('ok')} {spoken.get('voice', {}).get('wav')}")
        print(f"confirmations clicked in the shell: {clicked}")
        print(f"browser now: {page['url']} | {page['title']} | {page.get('outbound_links')} result links")
        print(f"end to end {total:.0f}s; screenshots in {shots}")
        ok = (e <= 0.25 and words_ok and bool(search and search["verified"]) and page_ok
              and spoken.get("ok") is True and any(c == ".yes" for _, c in clicked))
    finally:
        if orch is not None:
            orch.browser.close()
        for pr in procs:
            launch.stop(pr)
    print("=== phase 7:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
