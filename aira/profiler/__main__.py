"""python -m aira.profiler [--launch] [--keep] [--port N] [--out FILE]

Measures the machine, chooses tier and slots, and (with --launch) starts llama-server,
measures tokens/sec, and steps down a tier if generation is below --min-tps.
Prints the profile as JSON and writes it to --out.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import launch
from .plan import TIER_ORDER, load_catalogue, plan
from .probe import probe


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aira.profiler")
    ap.add_argument("--models", type=Path, default=launch.WORK / "models")
    ap.add_argument("--ctx-per-slot", type=int, default=8192)
    ap.add_argument("--launch", action="store_true", help="start llama-server and benchmark it")
    ap.add_argument("--keep", action="store_true", help="leave the server running after --launch")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--min-tps", type=float, default=5.0,
                    help="step down a tier if generation is slower than this")
    ap.add_argument("--max-slots", type=int, default=None, help="cap the slot count")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    hw = probe()
    p = plan(hw, args.models, args.ctx_per_slot, max_slots=args.max_slots)
    profile: dict = {"hardware": hw.to_dict(), "plan": p.to_dict()}

    if args.launch:
        catalogue = load_catalogue(args.models)
        log_dir = Path(tempfile.gettempdir())
        while True:
            log_path = log_dir / f"aira-llama-{p.tier}.log"
            proc = launch.start(p, args.port, log_path)
            try:
                b = launch.bench(args.port)
            except Exception:
                launch.stop(proc)
                raise
            profile["bench"] = b.to_dict()
            profile["server_log"] = str(log_path)
            profile["offload"] = launch.offload_summary(log_path)
            profile["plan"] = p.to_dict()
            smaller = [t for t in TIER_ORDER[:TIER_ORDER.index(p.tier)] if t in catalogue]
            if b.gen_tps >= args.min_tps or not smaller:
                if b.gen_tps < args.min_tps:
                    p.reasons.append(f"{p.tier} gives {b.gen_tps:.1f} tok/s, below {args.min_tps}, "
                                     "but no smaller model is available")
                    profile["plan"] = p.to_dict()
                break
            launch.stop(proc)
            note = (f"measured {p.tier} at {b.gen_tps:.1f} tok/s, below {args.min_tps}; "
                    f"stepping down to {smaller[-1]}")
            p = plan(hw, args.models, args.ctx_per_slot, max_tier=smaller[-1], reasons=[note],
                     max_slots=args.max_slots)
        profile["server_command"] = launch.server_command(p, args.port)
        if args.keep:
            profile["server_pid"] = proc.pid
        else:
            launch.stop(proc)

    text = json.dumps(profile, indent=2, default=str)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
