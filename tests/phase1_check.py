#!/usr/bin/env python3
"""Phase 1 check: the profiler picks the right tier here and under simulated memory caps.

Cases (each runs `python -m aira.profiler --launch`, so the server really starts and answers):
  here        no simulation                         expect 9B on GPU
  cap-5G      cgroup MemoryMax=5G, GPU hidden       expect 2B
  cap-7G      cgroup MemoryMax=7G, GPU hidden       expect 4B
  vram-24G    fake 24 GiB GPU (no launch)           expect rule 27B, falls back to 9B (27B not downloaded)
Plus: per-slot memory estimate vs the KV/RS buffer sizes llama-server reports.

Memory caps are real cgroup limits via `systemd-run --scope -p MemoryMax=...`, so the
server itself also runs under the cap. Run as root (systemd-run --scope needs it here).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from aira.profiler import launch  # noqa: E402
from aira.profiler.gguf import read_metadata  # noqa: E402
from aira.profiler.plan import per_slot_bytes  # noqa: E402

PY = sys.executable
MODELS = launch.WORK / "models"


def run_profiler(case: str, *, cap: str | None, env_extra: dict, launch_server: bool, port: int) -> dict:
    cmd = [PY, "-m", "aira.profiler", "--port", str(port)]
    if launch_server:
        cmd.append("--launch")
    if cap:
        cmd = ["systemd-run", "--quiet", "--scope", "--unit", f"aira-p1-{case}-{os.getpid()}",
               "-p", f"MemoryMax={cap}", "-p", "MemorySwapMax=0", *cmd]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **env_extra)
    t0 = time.monotonic()
    r = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, timeout=900)
    dt = time.monotonic() - t0
    if r.returncode != 0:
        raise RuntimeError(f"profiler failed (exit {r.returncode}):\n{r.stderr[-2000:]}")
    prof = json.loads(r.stdout)
    prof["_seconds"] = dt
    return prof


def check_case(case: str, expect_tier: str, *, cap=None, env_extra=None, launch_server=True,
               expect_rule=None, port=18200) -> bool:
    print(f"\n=== {case}: expect {expect_tier}", flush=True)
    try:
        prof = run_profiler(case, cap=cap, env_extra=env_extra or {}, launch_server=launch_server, port=port)
    except Exception as exc:
        print(f"FAIL {exc}")
        return False
    hw, p = prof["hardware"], prof["plan"]
    gib = 2**30
    print(f"hardware: RAM {hw['ram_total']/gib:.2f} GiB (avail {hw['ram_available']/gib:.2f}), "
          f"VRAM {hw['vram_total']/gib:.2f} GiB, cores {hw['cpu_cores']}, simulated={hw['simulated']}, "
          f"notes={hw['notes']}")
    for r in p["reasons"]:
        print(f"  reason: {r}")
    print(f"plan: tier {p['tier']} (rule {p['tier_by_rule']}), slots {p['slots']}, gpu={p['use_gpu']}")
    ok = p["tier"] == expect_tier
    if expect_rule and p["tier_by_rule"] != expect_rule:
        print(f"FAIL rule tier {p['tier_by_rule']} != {expect_rule}")
        ok = False
    if launch_server:
        b = prof.get("bench", {})
        print(f"bench: {b.get('gen_tps', 0):.1f} tok/s generation, {b.get('prompt_tps', 0):.1f} tok/s prompt "
              f"({prof['_seconds']:.0f}s total)")
        for line in prof.get("offload", [])[:6]:
            print(f"  log: {line}")
        if not b.get("gen_tokens"):
            print("FAIL server produced no tokens")
            ok = False
    print("PASS" if ok else f"FAIL got {p['tier']}")
    return ok


def check_slot_estimate(model_file: str = "Qwen3.5-2B-Q4_K_M.gguf", slots: int = 2, ctx: int = 8192) -> bool:
    print(f"\n=== per-slot memory estimate vs llama-server ({model_file}, {slots} x {ctx})", flush=True)
    model = MODELS / model_file
    est = per_slot_bytes(read_metadata(model), ctx)
    binary = launch.WORK / "bin-cpu" / "llama-server"
    log = Path("/tmp/aira-p1-kvprobe.log")
    port = 18299
    env = dict(os.environ, LD_LIBRARY_PATH=str(binary.parent), CUDA_VISIBLE_DEVICES="")
    with log.open("w") as fh:
        proc = subprocess.Popen([str(binary), "-m", str(model), "--port", str(port), "--jinja",
                                 "--reasoning", "off", "-ngl", "0", "--parallel", str(slots),
                                 "--ctx-size", str(slots * ctx), "--no-kv-unified", "-lv", "4"],
                                env=env, stdout=fh, stderr=subprocess.STDOUT)
        try:
            for _ in range(120):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
                    break
                except Exception:
                    time.sleep(1)
        finally:
            launch.stop(proc)
    text = log.read_text(errors="replace")
    kv = sum(float(m) for m in re.findall(r"KV buffer size =\s+([\d.]+) MiB", text))
    rs = sum(float(m) for m in re.findall(r"RS buffer size =\s+([\d.]+) MiB", text))
    measured = (kv + rs) * 2**20 / slots
    err = abs(measured - est) / measured if measured else 1.0
    print(f"estimate {est/2**20:.2f} MiB/slot, measured {measured/2**20:.2f} MiB/slot "
          f"(KV {kv:.2f} + RS {rs:.2f} MiB for {slots} slots), error {err*100:.1f}%")
    ok = measured > 0 and err < 0.10
    print("PASS" if ok else "FAIL")
    return ok


CASES = {
    "here": lambda: check_case("here", "9B", expect_rule="9B", port=18201),
    "cap-5G": lambda: check_case("cap5", "2B", cap="5G", env_extra={"AIRUX_NO_GPU": "1"}, port=18202),
    "cap-7G": lambda: check_case("cap7", "4B", cap="7G", env_extra={"AIRUX_NO_GPU": "1"}, port=18203),
    "vram-24G": lambda: check_case("vram24", "9B", env_extra={"AIRUX_SIM_VRAM_MB": "24576"},
                                   launch_server=False, expect_rule="27B"),
    "slot-estimate": check_slot_estimate,
}


def main() -> int:
    names = sys.argv[1:] or list(CASES)
    results = {n: CASES[n]() for n in names}
    print("\n=== phase 1:", "PASS" if all(results.values()) else "FAIL", results)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
