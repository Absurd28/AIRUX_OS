#!/usr/bin/env python3
"""Phase 0 check: llama-server answers a test prompt.

For each model: start scripts/run_server.sh on a spare port, wait for /health,
send one chat request, and require a non-empty answer with no thinking output
(thinking must be off for tool calls). Stdlib only.

Usage: phase0_check.py [MODEL_FILE ...]   (default: the 2B and 9B models)
Exit code 0 only if every model passes.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORK = Path(os.environ.get("AIRUX_WORK", "/srv/airux-work"))
DEFAULT_MODELS = ["Qwen3.5-2B-Q4_K_M.gguf", "Qwen3.5-9B-Q4_K_M.gguf"]
HEALTH_TIMEOUT_S = 300
PROMPT = "Reply with one short sentence: what is 2 + 3?"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url: str, body: dict | None = None, timeout: float = 120) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_healthy(base: str, proc: subprocess.Popen) -> bool:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            if http_json(f"{base}/health", timeout=5).get("status") == "ok":
                return True
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(1)
    return False


def check_model(model_file: str) -> bool:
    model = WORK / "models" / model_file
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = Path("/tmp") / f"phase0_{model_file}.log"
    env = dict(os.environ, MODEL=str(model), PORT=str(port), CTX="4096")
    print(f"--- {model_file} on port {port} (log: {log_path})", flush=True)
    with log_path.open("w") as log:
        proc = subprocess.Popen(["bash", str(REPO / "scripts" / "run_server.sh")],
                                env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            if not wait_healthy(base, proc):
                print(f"FAIL server did not become healthy (exit={proc.poll()}); "
                      f"last log lines:\n{tail(log_path)}")
                return False
            t0 = time.monotonic()
            resp = http_json(f"{base}/v1/chat/completions", {
                "messages": [{"role": "user", "content": PROMPT}],
                "max_tokens": 64,
                "temperature": 0,
            })
            dt = time.monotonic() - t0
            msg = resp["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning_content") or "").strip()
            usage = resp.get("usage", {})
            timings = resp.get("timings", {})
            print(f"answer: {content!r}")
            print(f"tokens: {usage.get('completion_tokens')} in {dt:.1f}s; "
                  f"gen {timings.get('predicted_per_second', 0):.1f} tok/s")
            ok = True
            if not content:
                print("FAIL empty answer")
                ok = False
            if reasoning or "<think>" in content:
                print(f"FAIL thinking output present (reasoning={reasoning[:80]!r})")
                ok = False
            offload = [l for l in log_path.read_text(errors="replace").splitlines()
                       if "offloaded" in l or "CUDA0" in l and "buffer size" in l]
            for line in offload[:3]:
                print(f"log: {line.strip()}")
            print("PASS" if ok else "FAIL")
            return ok
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()


def tail(path: Path, n: int = 15) -> str:
    return "\n".join(path.read_text(errors="replace").splitlines()[-n:])


def main() -> int:
    models = sys.argv[1:] or DEFAULT_MODELS
    results = {m: check_model(m) for m in models}
    print("=== phase 0:", "PASS" if all(results.values()) else "FAIL", results)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
