#!/usr/bin/env python3
"""Persona demo (informational, not a phase check): a few small-talk and task turns with the real
model, names Aryan / boss. Prints what AIRA shows and what she would say aloud."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from fixture import build_fixture, start_server  # noqa: E402

COMMANDS = ["Hey AIRA, how are you doing today?", "Tell me a joke about debugging.",
            "I'm exhausted, I've been fighting a CUDA error all afternoon.", "Who are you?",
            "How much free disk space do I have in my home folder?", "Delete old.log in my home folder.",
            "What's the weather like?", "Thanks, you're a lifesaver.", "Good night!"]


def main() -> int:
    home = build_fixture(Path(tempfile.gettempdir()) / "aira-persona")
    os.environ.update(HOME=str(home), AIRA_DATA=str(home.parent / "aira-data"), AIRA_USER_NAMES="Aryan,boss")
    from aira.config import Config
    from aira.orchestrator import Orchestrator
    from aira.profiler import launch
    from aira.voice import spoken_text
    server, note = start_server(19200, "aira-persona-server.log")
    try:
        orch = Orchestrator(Config(), lambda *a: False)
        print(f"model {note}; names {orch.persona.names}\n")
        for c in COMMANDS:
            t = orch.handle(c)
            print(f"> {c}\n  [{t.kind}] shown: {t.reply!r}\n  said aloud: {spoken_text(t.reply)!r}\n", flush=True)
    finally:
        launch.stop(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
