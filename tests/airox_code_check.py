#!/usr/bin/env python3
"""AIROX Code check: AIRA inside AIROX Code, against AIRA's real local model.

Builds a throwaway git project whose calc.py has a bug, starts llama-server with the profiler's
plan, and runs AIROX Code (the staged package) on a virtual display (xvfb-run) with the
integration suite in airox-code/extension/aira/test/integration. Pass requires:
  1. inline: a ghost-text suggestion that completes mul() with a return;
  2. chat: an answer about the bug that cites calc.py at lines that exist (grounding check);
  3. edit_review: "Fix" proposes a change, the file is untouched until accepted, then sub() is
     fixed and read back;
  4. commit_message: a commit message is written for the change (summary <= 72 chars).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))
WORK = Path(os.environ.get("AIRUX_WORK", "/srv/airux-work"))
APP = WORK / "build/deb/airox-code/usr/share/airox-code"
EXT = APP / "resources/app/extensions/aira"
SUITE = REPO / "airox-code/extension/aira/test/integration/index.js"


def make_project(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / "calc.py").write_text(
        '"""Small calculator."""\n\n\ndef add(a, b):\n    return a + b\n\n\n'
        'def sub(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    \n')
    env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "calc"]):
        subprocess.run(cmd, cwd=root, check=True, env=env)


def main() -> int:
    if not (APP / "codium").exists():
        print(f"no staged AIROX Code at {APP}; run scripts/build_airox.sh first")
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="airox-it-"))
    proj = tmp / "project"
    make_project(proj)
    results = tmp / "results.json"

    from fixture import start_server
    server, note = (None, os.environ["AIRA_SERVER"]) if "AIRA_SERVER" in os.environ \
        else start_server(8080, "airox-it-server.log")
    try:
        env = dict(os.environ, AIRA_IT_RESULTS=str(results), DONT_PROMPT_WSL_INSTALL="1")
        env.pop("WAYLAND_DISPLAY", None)
        cmd = ["xvfb-run", "-a", "-s", "-screen 0 1600x1000x24", str(APP / "codium"),
               "--no-sandbox", "--disable-gpu", "--disable-workspace-trust", "--skip-welcome",
               f"--user-data-dir={tmp / 'user'}", f"--extensions-dir={tmp / 'exts'}",
               f"--extensionDevelopmentPath={EXT}", f"--extensionTestsPath={SUITE}", str(proj)]
        log = tmp / "airox-code.log"
        try:
            with open(log, "w") as lf:
                subprocess.run(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT, timeout=1200)
        except subprocess.TimeoutExpired:
            print("AIROX Code did not finish in 20 minutes")
        print(f"--- AIROX Code log ({log}), test lines and errors:")
        for line in log.read_text(errors="replace").splitlines():
            if "[aira-it]" in line or "rror" in line:
                print("   ", line[:300])
        if not results.exists():
            print("AIROX Code did not produce results")
            return 1
    finally:
        if server is not None:
            from aira.profiler import launch
            launch.stop(server)

    r = json.loads(results.read_text())
    print(f"product: {r.pop('product', '?')}; model {note}")
    passed = 0
    for name in ("inline", "chat", "edit_review", "commit_message"):
        x = r.get(name, {"ok": False, "error": "missing"})
        ok = x.get("ok") and x.get("pass")
        passed += bool(ok)
        detail = {k: v for k, v in x.items() if k not in ("ok", "pass")}
        print(f"{'PASS' if ok else 'FAIL'} {name} [{x.get('seconds', '?')}s]")
        for k, v in detail.items():
            if k != "seconds":
                print(f"    {k}: {str(v)[:600]!s}")
    print(f"=== AIROX Code: {passed}/4", "PASS" if passed == 4 else "FAIL")
    return 0 if passed == 4 else 1


if __name__ == "__main__":
    sys.exit(main())
