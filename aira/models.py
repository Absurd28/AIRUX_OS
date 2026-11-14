"""aira-models: list, verify or download AIRA's model files (models/MANIFEST.toml).

    aira-models list
    aira-models fetch qwen3.5-9b         (downloads; run with sudo on the installed OS)
    aira-models verify

Downloads resume and are checked against the manifest's sha256. The first-boot profiler only
recommends a bigger model; downloading always needs the user to ask for it.
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
CHUNK = 1 << 20


def models_dir() -> Path:
    return Path(os.environ.get("AIRUX_WORK", "/opt/aira")) / "models"


def manifest() -> list[dict]:
    return tomllib.loads(MANIFEST.read_text())["model"]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: Path, progress=print) -> None:
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "aira-models/0.1"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=60) as resp:
        if have and resp.status != 206:
            have = 0
        total = have + int(resp.headers.get("Content-Length", 0))
        done, last = have, -1
        with part.open("ab" if have else "wb") as out:
            while block := resp.read(CHUNK):
                out.write(block)
                done += len(block)
                pct = int(done * 100 / total) if total else 0
                if pct != last and pct % 10 == 0:
                    progress(f"  {dest.name}: {pct}% ({done / 1e9:.2f} GB)")
                    last = pct
    part.rename(dest)


def fetch(entry: dict, dest_dir: Path, progress=print) -> tuple[bool, str]:
    path = dest_dir / entry["file"]
    if not path.exists():
        progress(f"downloading {entry['name']} from {entry['url']}")
        try:
            download(entry["url"], path, progress)
        except Exception as exc:
            return False, f"download failed: {exc}"
    want = entry.get("sha256", "")
    got = sha256_of(path)
    if want and got != want:
        return False, f"sha256 mismatch for {path.name}: got {got}"
    return True, f"{path} ok"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aira-models")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    f = sub.add_parser("fetch")
    f.add_argument("names", nargs="+")
    sub.add_parser("verify")
    ap.add_argument("--dest", type=Path, default=None)
    args = ap.parse_args(argv)
    dest = args.dest or models_dir()
    entries = manifest()
    if args.cmd == "list":
        for e in entries:
            have = (dest / e["file"]).exists()
            print(f"{'installed' if have else 'available':9}  {e['name']:24} {e.get('size', 0) / 1e9:5.1f} GB  "
                  f"{e.get('license', '')}")
        return 0
    if args.cmd == "verify":
        bad = 0
        for e in entries:
            p = dest / e["file"]
            if p.exists():
                ok, msg = fetch(e, dest)
                print(("OK   " if ok else "FAIL ") + msg)
                bad += not ok
        return 1 if bad else 0
    dest.mkdir(parents=True, exist_ok=True)
    by_name = {e["name"]: e for e in entries}
    rc = 0
    for n in args.names:
        if n not in by_name:
            print(f"no model called {n!r}; see aira-models list")
            rc = 1
            continue
        ok, msg = fetch(by_name[n], dest)
        print(("OK   " if ok else "FAIL ") + msg)
        rc |= not ok
    return rc


if __name__ == "__main__":
    sys.exit(main())
