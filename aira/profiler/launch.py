"""Launch llama-server for a Plan and measure tokens per second."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .plan import Plan

WORK = Path(os.environ.get("AIRUX_WORK", "/srv/airux-work"))
BENCH_PROMPT = ("List the planets of the solar system in order from the Sun, "
                "one per line, with one short fact about each.")


@dataclass
class Bench:
    gen_tps: float      # generated tokens per second
    prompt_tps: float   # prompt tokens per second
    gen_tokens: int

    def to_dict(self) -> dict:
        return asdict(self)


def server_binary(use_gpu: bool) -> Path:
    """CUDA build when a GPU is in use and the build exists, else the portable CPU build."""
    cuda = WORK / "bin" / "llama-server"
    cpu = WORK / "bin-cpu" / "llama-server"
    if use_gpu and cuda.is_file():
        return cuda
    if cpu.is_file():
        return cpu
    if cuda.is_file():
        return cuda
    raise FileNotFoundError(f"llama-server not found in {cuda.parent} or {cpu.parent}")


FIT_TARGET_MIB = int(os.environ.get("AIRUX_FIT_TARGET_MIB", "1024"))  # llama.cpp default 1024


def server_command(p: Plan, port: int, host: str = "127.0.0.1") -> list[str]:
    binary = server_binary(p.use_gpu)
    cmd = [
        str(binary), "--model", p.model_path, "--host", host, "--port", str(port),
        "--jinja", "--reasoning", "off",
        "--parallel", str(p.slots), "--ctx-size", str(p.slots * p.ctx_per_slot),
        "--no-kv-unified",  # each slot keeps its own ctx_per_slot window
        "--cache-ram", str(p.cache_ram_mib),  # bounded; llama.cpp's default (8192 MiB) can OOM
    ]
    if not p.use_gpu:
        cmd += ["--n-gpu-layers", "0"]
    else:
        # --n-gpu-layers is left unset so llama.cpp's --fit (default on) offloads as many
        # layers as fit in free VRAM, keeping FIT_TARGET_MIB free for the desktop
        cmd += ["--fit-target", str(FIT_TARGET_MIB)]
    return cmd


def server_env(p: Plan) -> dict[str, str]:
    env = dict(os.environ)
    lib = str(Path(server_binary(p.use_gpu)).parent)
    env["LD_LIBRARY_PATH"] = ":".join(x for x in (lib, "/usr/lib/wsl/lib", env.get("LD_LIBRARY_PATH")) if x)
    if not p.use_gpu:
        env["CUDA_VISIBLE_DEVICES"] = ""
    return env


def _get(url: str, timeout: float = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url: str, body: dict, timeout: float = 300) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def start(p: Plan, port: int, log_path: Path, health_timeout: float = 300) -> subprocess.Popen:
    """Start llama-server and wait until /health reports ok. Raises on failure."""
    log = log_path.open("w")
    proc = subprocess.Popen(server_command(p, port), env=server_env(p),
                            stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + health_timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited with code {proc.returncode}; see {log_path}")
        try:
            if _get(f"http://127.0.0.1:{port}/health").get("status") == "ok":
                return proc
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(1)
    stop(proc)
    raise TimeoutError(f"llama-server not healthy after {health_timeout:.0f}s; see {log_path}")


EMBED_MODEL = "nomic-embed-text-v1.5.Q8_0.gguf"


def start_embedder(port: int, log_path: Path, health_timeout: float = 120) -> subprocess.Popen:
    """Small CPU-only llama-server for embeddings (memory vault vector search)."""
    binary = WORK / "bin-cpu" / "llama-server"
    if not binary.is_file():
        binary = server_binary(False)
    env = dict(os.environ, LD_LIBRARY_PATH=str(binary.parent), CUDA_VISIBLE_DEVICES="")
    log = log_path.open("w")
    proc = subprocess.Popen([str(binary), "--model", str(WORK / "models" / EMBED_MODEL), "--host", "127.0.0.1",
                             "--port", str(port), "--embedding", "--n-gpu-layers", "0", "--ctx-size", "2048",
                             "--threads", "2", "--cache-ram", "0"], env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + health_timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"embedding server exited with code {proc.returncode}; see {log_path}")
        try:
            if _get(f"http://127.0.0.1:{port}/health").get("status") == "ok":
                return proc
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    stop(proc)
    raise TimeoutError(f"embedding server not healthy after {health_timeout:.0f}s; see {log_path}")


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def bench(port: int, max_tokens: int = 128) -> Bench:
    """Measure generation speed through the running server (warm-up request first)."""
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    _post(url, {"messages": [{"role": "user", "content": "Say ok."}], "max_tokens": 4})
    r = _post(url, {"messages": [{"role": "user", "content": BENCH_PROMPT}],
                    "max_tokens": max_tokens, "temperature": 0})
    t = r.get("timings", {})
    if "predicted_per_second" not in t:
        raise RuntimeError("server response has no timings; cannot measure tokens/sec")
    return Bench(gen_tps=float(t["predicted_per_second"]),
                 prompt_tps=float(t.get("prompt_per_second", 0.0)),
                 gen_tokens=int(t.get("predicted_n", 0)))


def offload_summary(log_path: Path) -> list[str]:
    """Lines from the server log that say where the model and caches went."""
    keep = ("offloaded", "KV buffer size", "RS buffer size", "model buffer size", "fit", "n_slots")
    lines = log_path.read_text(errors="replace").splitlines()
    return [l.strip() for l in lines if any(k in l for k in keep)][:20]
