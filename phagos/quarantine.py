"""Quarantine: threats are moved here, made harmless, and can be put back.

A quarantined file is compressed and XOR-masked (so it is neither runnable nor recognisable as the
original by anything that scans this folder), stored root-only, with its original path, owner, mode
and SHA-256 next to it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
import zlib
from pathlib import Path

MASK = hashlib.sha256(b"phagos quarantine mask v1").digest()


class QuarantineError(Exception):
    pass


def _mask(data: bytes) -> bytes:
    key = (MASK * (len(data) // len(MASK) + 1))[:len(data)]
    return bytes(a ^ b for a, b in zip(data, key)) if len(data) < 1 << 16 else \
        (int.from_bytes(data, "little") ^ int.from_bytes(key, "little")).to_bytes(len(data), "little")


class Quarantine:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def add(self, path: str, threat: str, level: str, engine: str, extra: dict | None = None) -> dict:
        src = Path(path)
        try:
            st = src.lstat()
            data = src.read_bytes()
        except OSError as exc:
            raise QuarantineError(f"cannot read {path}: {exc.strerror or exc}") from exc
        qid = uuid.uuid4().hex[:12]
        meta = {"id": qid, "path": str(src), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                "mode": st.st_mode & 0o7777, "uid": st.st_uid, "gid": st.st_gid, "threat": threat,
                "level": level, "engine": engine, "ts": time.time(), **(extra or {})}
        blob = self.root / f"{qid}.q"
        fd = os.open(blob, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(_mask(zlib.compress(data, 6)))
        (self.root / f"{qid}.json").write_text(json.dumps(meta))
        os.chmod(self.root / f"{qid}.json", 0o600)
        try:
            src.unlink()
        except OSError as exc:
            blob.unlink(missing_ok=True)
            (self.root / f"{qid}.json").unlink(missing_ok=True)
            raise QuarantineError(f"cannot remove {path}: {exc.strerror or exc}") from exc
        return meta

    def items(self) -> list[dict]:
        out = []
        for m in sorted(self.root.glob("*.json")):
            try:
                out.append(json.loads(m.read_text()))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda m: -m["ts"])

    def get(self, qid: str) -> dict:
        if not qid.isalnum():
            raise QuarantineError("no such quarantined item")
        m = self.root / f"{qid}.json"
        if not m.exists():
            raise QuarantineError(f"no quarantined item {qid}")
        return json.loads(m.read_text())

    def restore(self, qid: str, overwrite: bool = False) -> dict:
        meta = self.get(qid)
        dst = Path(meta["path"])
        if dst.exists() and not overwrite:
            raise QuarantineError(f"{dst} exists again; not overwriting it")
        data = zlib.decompress(_mask((self.root / f"{qid}.q").read_bytes()))
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise QuarantineError("the quarantined copy is damaged (checksum mismatch)")
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.phagos-restore")
        tmp.write_bytes(data)
        try:
            os.chown(tmp, meta["uid"], meta["gid"])
        except PermissionError:
            pass                                   # tests and non-root use: keep the current owner
        os.chmod(tmp, meta["mode"])
        tmp.replace(dst)
        self.delete(qid)
        return meta

    def delete(self, qid: str) -> None:
        self.get(qid)
        (self.root / f"{qid}.q").unlink(missing_ok=True)
        (self.root / f"{qid}.json").unlink(missing_ok=True)
