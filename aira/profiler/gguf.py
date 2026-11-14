"""Minimal GGUF metadata reader (stdlib only).

Reads the key/value header of a GGUF file without loading tensors. Large arrays
(tokenizer vocab etc.) are skipped unless asked for, so reading takes milliseconds.
Format: https://github.com/ggml-org/ggml/blob/master/docs/gguf.md
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, BinaryIO

GGUF_MAGIC = b"GGUF"

# value type id -> struct format (scalars)
_SCALARS = {
    0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
    6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d",
}
_STRING, _ARRAY = 8, 9
MAX_ARRAY_KEEP = 64  # arrays longer than this are summarised, not returned


class GGUFError(ValueError):
    pass


def _read(f: BinaryIO, fmt: str) -> Any:
    size = struct.calcsize(fmt)
    data = f.read(size)
    if len(data) != size:
        raise GGUFError("unexpected end of file")
    return struct.unpack(fmt, data)[0]


def _read_str(f: BinaryIO) -> str:
    n = _read(f, "<Q")
    data = f.read(n)
    if len(data) != n:
        raise GGUFError("unexpected end of file in string")
    return data.decode("utf-8", errors="replace")


def _read_value(f: BinaryIO, vtype: int) -> Any:
    if vtype in _SCALARS:
        return _read(f, _SCALARS[vtype])
    if vtype == _STRING:
        return _read_str(f)
    if vtype == _ARRAY:
        itype = _read(f, "<I")
        n = _read(f, "<Q")
        if n <= MAX_ARRAY_KEEP:
            return [_read_value(f, itype) for _ in range(n)]
        # skip long arrays quickly
        if itype in _SCALARS:
            f.seek(struct.calcsize(_SCALARS[itype]) * n, 1)
        else:
            for _ in range(n):
                _read_value(f, itype)
        return {"array_len": n, "item_type": itype}
    raise GGUFError(f"unknown GGUF value type {vtype}")


def read_metadata(path: str | Path) -> dict[str, Any]:
    """Return the GGUF key/value metadata as a dict."""
    with open(path, "rb") as f:
        if f.read(4) != GGUF_MAGIC:
            raise GGUFError(f"{path}: not a GGUF file")
        version = _read(f, "<I")
        if version < 2:
            raise GGUFError(f"{path}: GGUF version {version} not supported")
        _tensor_count = _read(f, "<Q")
        kv_count = _read(f, "<Q")
        meta: dict[str, Any] = {"GGUF.version": version}
        for _ in range(kv_count):
            key = _read_str(f)
            vtype = _read(f, "<I")
            meta[key] = _read_value(f, vtype)
        return meta
