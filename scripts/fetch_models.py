#!/usr/bin/env python3
"""Download and verify the model files listed in models/MANIFEST.toml.

Stdlib only. Resumes partial downloads (.part files). Verifies sha256; for
"tofu" entries with an empty hash it prints the hash so it can be recorded.

Usage: fetch_models.py [--dest DIR] [--only NAME ...] [--verify-only]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tomllib
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "models" / "MANIFEST.toml"
DEFAULT_DEST = Path(os.environ.get("AIRUX_WORK", "/srv/airux-work")) / "models"
CHUNK = 1 << 20


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "airux-fetch/0.1"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=60) as resp:
        if have and resp.status != 206:  # server ignored Range: start over
            have = 0
        total = have + int(resp.headers.get("Content-Length", 0))
        mode = "ab" if have else "wb"
        done = have
        last_pct = -1
        with part.open(mode) as out:
            while block := resp.read(CHUNK):
                out.write(block)
                done += len(block)
                pct = int(done * 100 / total) if total else 0
                if pct != last_pct and pct % 10 == 0:
                    print(f"  {dest.name}: {pct}% ({done / 1e9:.2f} GB)", flush=True)
                    last_pct = pct
    part.rename(dest)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    entries = tomllib.loads(MANIFEST.read_text())["model"]
    if args.only:
        entries = [e for e in entries if e["name"] in args.only]
    args.dest.mkdir(parents=True, exist_ok=True)

    failures = 0
    for e in entries:
        path = args.dest / e["file"]
        if not path.exists():
            if args.verify_only:
                print(f"MISSING  {e['name']}: {path}")
                failures += 1
                continue
            print(f"GET      {e['name']} <- {e['url']}", flush=True)
            try:
                download(e["url"], path)
            except Exception as exc:  # report plainly, keep going with the rest
                print(f"FAIL     {e['name']}: download error: {exc}")
                failures += 1
                continue
        digest = sha256_of(path)
        want = e.get("sha256", "")
        if not want:
            print(f"TOFU     {e['name']}: sha256 = {digest}  (record this in MANIFEST.toml)")
        elif digest == want:
            print(f"OK       {e['name']}: sha256 verified")
        else:
            print(f"FAIL     {e['name']}: sha256 mismatch, got {digest}, want {want}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
