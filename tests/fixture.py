"""Throwaway home folder shared by the phase 2 and phase 3 checks."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def build_fixture(root: Path) -> Path:
    if root.exists():
        shutil.rmtree(root)
    home = root / "home"
    for d in ("notes", "data", "Documents", "projects/demo/tests", "envs"):
        (home / d).mkdir(parents=True)
    (home / "notes" / "lab-notes.md").write_text(
        "# Lab notes\n\n2026-11-10: the transformer run diverged at step 400 (divergence after LR warmup).\n")
    (home / "notes" / "ideas.txt").write_text("Try sparse attention for long documents.\n")
    (home / "data" / "results.csv").write_text("epoch,loss,accuracy\n1,2.3,0.41\n2,1.7,0.55\n3,1.2,0.68\n")
    (home / "Documents" / "report.txt").write_text("Quarterly report draft.\n")
    (home / "old.log").write_text("old log line\n")
    demo = home / "projects" / "demo"
    (demo / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a + b  # bug\n")
    (demo / "tests" / "test_calc.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n"
        "from calc import add, sub\n\ndef test_add():\n    assert add(2, 3) == 5\n\n"
        "def test_sub():\n    assert sub(5, 3) == 2\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
    for i, msg in enumerate(["initial calc", "add tests", "fix add", "docs", "tweak", "more"]):
        (demo / "CHANGELOG").write_text(f"{i}\n")
        if i == 0:
            subprocess.run(["git", "init", "-q", "-b", "main", str(demo)], check=True)
        subprocess.run(["git", "-C", str(demo), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(demo), "commit", "-q", "-m", msg], check=True, env=env)
    return home


def start_server(port: int, log_name: str):
    """Start llama-server with the profiler's plan (AIRA_MAX_SLOTS caps slots). Returns (proc, note)."""
    import tempfile

    from aira.profiler import launch
    from aira.profiler.plan import plan
    from aira.profiler.probe import probe
    max_slots = int(os.environ["AIRA_MAX_SLOTS"]) if os.environ.get("AIRA_MAX_SLOTS") else None
    p = plan(probe(), launch.WORK / "models", max_slots=max_slots)
    for reason in p.reasons:
        print(f"  plan: {reason}")
    print(f"starting llama-server: {p.tier}, {p.slots} slots, gpu={p.use_gpu}", flush=True)
    proc = launch.start(p, port, Path(tempfile.gettempdir()) / log_name)
    os.environ["AIRA_SERVER"] = f"http://127.0.0.1:{port}"
    return proc, f"{p.tier} ({p.slots} slots)"
