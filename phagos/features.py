"""What Phagos sees in a file: a small feature vector in [0, 1]^N, shared by every engine.

The immune engines (Negative Selection, Clonal Selection) work on these vectors, so a feature must
mean the same on every computer and be cheap to compute (only the first and last 256 KiB are read).
"""
from __future__ import annotations

import math
from collections import Counter
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

HEAD = 256 * 1024
TAIL = 64 * 1024

# feature order (the vector's dimensions); keep stable: saved detectors depend on it
NAMES = [
    "entropy",          # Shannon entropy of the sampled bytes / 8
    "size",             # log2(size) / 32
    "is_elf",
    "is_script",        # starts with #!
    "elf_static",       # ELF without a program interpreter (statically linked)
    "elf_stripped",     # ELF without .symtab
    "packed",           # packer marker (UPX!) or very high entropy in an ELF
    "printable",        # share of printable ASCII
    "suspicious",       # suspicious strings found (see SUSPICIOUS), saturating at 6
    "net_strings",      # IP:port / URL strings, saturating at 8
    "risky_place",      # in /tmp, /dev/shm, /var/tmp or /run/user (not hidden folders: ~/.local/bin is normal)
    "exec_bit",
]
DIM = len(NAMES)

SUSPICIOUS = [rb"/dev/tcp/", rb"\bnc\s+-e\b", rb"\bsocat\b.*exec", rb"LD_PRELOAD", rb"/etc/ld\.so\.preload",
              rb"\bchattr\s+\+i\b", rb"stratum\+tcp://", rb"\bxmrig\b", rb"\bmkfifo\b", rb"base64\s+-d\s*\|\s*(ba)?sh",
              rb"curl[^\n|]{0,200}\|\s*(ba)?sh", rb"wget[^\n|]{0,200}\|\s*(ba)?sh", rb"authorized_keys",
              rb"/proc/self/exe", rb"ptrace", rb"crontab\s+-", rb"\bkillall\b", rb"history\s+-c", rb"iptables\s+-F"]
_SUS = [re.compile(p, re.I) for p in SUSPICIOUS]
_NET = re.compile(rb"\b\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}\b|https?://[\x21-\x7e]{4,80}")
_PRINTABLE = bytes(range(32, 127)) + bytes([9, 10, 13])
RISKY = ("/tmp/", "/dev/shm/", "/var/tmp/", "/run/user/")


@dataclass
class FileInfo:
    path: str
    size: int
    mode: int
    head: bytes
    tail: bytes
    vector: list[float] = field(default_factory=list)
    traits: dict = field(default_factory=dict)

    @property
    def data(self) -> bytes:
        return self.head + (self.tail if self.size > HEAD else b"")


def entropy(data: bytes) -> float:
    """Shannon entropy in bits per byte (0..8)."""
    if not data:
        return 0.0
    n = len(data)
    return -sum(c / n * math.log2(c / n) for c in Counter(data).values())


def _elf_traits(head: bytes) -> dict:
    """Interpreter and .symtab from the ELF headers in the first bytes (64-bit and 32-bit, little-endian)."""
    t = {"interp": False, "symtab": None}
    if len(head) < 64 or head[:4] != b"\x7fELF":
        return t
    is64 = head[4] == 2
    try:
        if is64:
            phoff = int.from_bytes(head[32:40], "little"); phentsize = int.from_bytes(head[54:56], "little")
            phnum = int.from_bytes(head[56:58], "little")
            shoff = int.from_bytes(head[40:48], "little"); shentsize = int.from_bytes(head[58:60], "little")
            shnum = int.from_bytes(head[60:62], "little")
        else:
            phoff = int.from_bytes(head[28:32], "little"); phentsize = int.from_bytes(head[42:44], "little")
            phnum = int.from_bytes(head[44:46], "little")
            shoff = int.from_bytes(head[32:36], "little"); shentsize = int.from_bytes(head[46:48], "little")
            shnum = int.from_bytes(head[48:50], "little")
        for i in range(min(phnum, 64)):
            off = phoff + i * phentsize
            if off + 4 <= len(head) and int.from_bytes(head[off:off + 4], "little") == 3:   # PT_INTERP
                t["interp"] = True
        # section types, if the section table happens to be in the sampled bytes (it is at the end of
        # the file, so only for small files); unknown otherwise
        if shnum and shoff + shnum * shentsize <= len(head):
            t["symtab"] = any(int.from_bytes(head[shoff + i * shentsize + 4: shoff + i * shentsize + 8], "little") == 2
                              for i in range(min(shnum, 256)))
    except (ValueError, IndexError):
        pass
    return t


def read(path: str | os.PathLike, fd: int | None = None) -> FileInfo | None:
    """Sample a regular file (by path, or through an already open descriptor). None if not readable."""
    try:
        if fd is not None:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return None
            head = os.pread(fd, HEAD, 0)
            tail = os.pread(fd, TAIL, max(0, st.st_size - TAIL)) if st.st_size > HEAD else b""
        else:
            st = os.stat(path)
            if not stat.S_ISREG(st.st_mode):
                return None
            with open(path, "rb") as f:
                head = f.read(HEAD)
                if st.st_size > HEAD:
                    f.seek(max(0, st.st_size - TAIL))
                    tail = f.read(TAIL)
                else:
                    tail = b""
    except OSError:
        return None
    info = FileInfo(str(path), st.st_size, st.st_mode, head, tail)
    info.vector, info.traits = vectorize(info)
    return info


def vectorize(info: FileInfo) -> tuple[list[float], dict]:
    data = info.data
    ent = entropy(data)
    is_elf = data[:4] == b"\x7fELF"
    is_script = data[:2] == b"#!"
    elf = _elf_traits(info.head) if is_elf else {"interp": False, "symtab": None}
    upx = is_elf and (b"UPX!" in data[:4096] or b"$Info: This file is packed with the UPX" in data)
    printable = 1 - len(data.translate(None, _PRINTABLE)) / len(data) if data else 0.0
    sus = [p.pattern.decode() for p in _SUS if p.search(data)]
    nets = len(_NET.findall(data[:HEAD]))
    risky = info.path.startswith(RISKY)
    vec = [
        ent / 8.0,
        min(1.0, math.log2(info.size + 1) / 32.0),
        1.0 if is_elf else 0.0,
        1.0 if is_script else 0.0,
        1.0 if is_elf and not elf["interp"] else 0.0,
        1.0 if is_elf and elf["symtab"] is False else 0.0,
        1.0 if upx or (is_elf and ent > 7.6) else 0.0,
        printable,
        min(1.0, len(sus) / 6.0),
        min(1.0, nets / 8.0),
        1.0 if risky else 0.0,
        1.0 if info.mode & 0o111 else 0.0,
    ]
    traits = {"entropy": round(ent, 3), "elf": is_elf, "script": is_script, "static": vec[4] == 1.0,
              "packed": vec[6] == 1.0, "suspicious": sus, "net_strings": nets, "risky_place": risky}
    return vec, traits


def distance(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
