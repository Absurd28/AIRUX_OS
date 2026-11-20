"""Phagos's own signature engine: rules written in TOML, matched with Python's regex engine.

A rule file holds [[rule]] tables:

    [[rule]]
    name = "Linux.ReverseShell.DevTcp"
    severity = "malicious"            # or "suspicious"
    description = "bash reverse shell through /dev/tcp"
    text = ["/dev/tcp/"]              # literal byte strings
    hex = ["7f 45 4c 46"]             # hex bytes, spaces allowed
    regex = ['bash\\s+-i\\s*>&\\s*/dev/tcp/']
    condition = "any"                 # "any", "all", or a number: at least that many patterns
    magic = "elf"                     # optional: file must start as "elf", "script" or "text"
    paths = ["/etc/cron*", "*/autostart/*"]   # optional: only files under these globs
    max_size = 1048576                # optional

Plus a SHA-256 blocklist: one hex digest per line, '#' comments, in *.sha256 files.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SEVERITIES = ("suspicious", "malicious")


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    name: str
    severity: str
    description: str
    patterns: list[re.Pattern]
    condition: int            # minimum number of patterns that must match (0 = all)
    magic: str | None = None
    paths: list[str] = field(default_factory=list)
    max_size: int | None = None
    source: str = ""

    def applies(self, path: str, data: bytes, size: int) -> bool:
        if self.max_size is not None and size > self.max_size:
            return False
        if self.paths and not any(fnmatch.fnmatch(path, g) for g in self.paths):
            return False
        if self.magic == "elf" and data[:4] != b"\x7fELF":
            return False
        if self.magic == "script" and data[:2] != b"#!":
            return False
        if self.magic == "text" and b"\x00" in data[:4096]:
            return False
        return True

    def match(self, data: bytes) -> list[str]:
        """The patterns that hit, if enough of them hit for the rule's condition; else []."""
        hits = [p.pattern.decode("latin-1") for p in self.patterns if p.search(data)]
        need = len(self.patterns) if self.condition == 0 else self.condition
        return hits if len(hits) >= need else []


@dataclass
class Hit:
    rule: str
    severity: str
    description: str
    evidence: list[str]


def _compile_rule(r: dict, source: str) -> Rule:
    name = r.get("name")
    if not name or not isinstance(name, str):
        raise RuleError(f"{source}: a rule has no name")
    sev = r.get("severity", "malicious")
    if sev not in SEVERITIES:
        raise RuleError(f"{source}: {name}: severity must be one of {SEVERITIES}")
    pats: list[re.Pattern] = []
    try:
        for t in r.get("text", []):
            pats.append(re.compile(re.escape(t.encode("latin-1"))))
        for h in r.get("hex", []):
            pats.append(re.compile(re.escape(bytes.fromhex(h.replace(" ", "")))))
        for x in r.get("regex", []):
            pats.append(re.compile(x.encode("latin-1"), re.I | re.S))
    except (re.error, ValueError, UnicodeEncodeError) as exc:
        raise RuleError(f"{source}: {name}: bad pattern: {exc}") from exc
    if not pats:
        raise RuleError(f"{source}: {name}: no patterns")
    cond = r.get("condition", "any")
    if cond == "any":
        n = 1
    elif cond == "all":
        n = 0
    elif isinstance(cond, int) and 1 <= cond <= len(pats):
        n = cond
    else:
        raise RuleError(f"{source}: {name}: condition must be any, all or 1..{len(pats)}")
    magic = r.get("magic")
    if magic not in (None, "elf", "script", "text"):
        raise RuleError(f"{source}: {name}: magic must be elf, script or text")
    return Rule(name, sev, r.get("description", ""), pats, n, magic, list(r.get("paths", [])),
                r.get("max_size"), source)


class RuleSet:
    def __init__(self, rules: list[Rule] | None = None, blocklist: dict[str, str] | None = None):
        self.rules = rules or []
        self.blocklist = blocklist or {}     # sha256 -> name

    @classmethod
    def load(cls, *dirs: str | Path) -> "RuleSet":
        rules, block = [], {}
        for d in dirs:
            d = Path(d)
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.toml")):
                try:
                    doc = tomllib.loads(f.read_text(encoding="utf-8"))
                except tomllib.TOMLDecodeError as exc:
                    raise RuleError(f"{f}: {exc}") from exc
                rules += [_compile_rule(r, f.name) for r in doc.get("rule", [])]
            for f in sorted(d.glob("*.sha256")):
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.split("#", 1)[0].strip()
                    if not line:
                        continue
                    digest, _, name = line.partition(" ")
                    if len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
                        raise RuleError(f"{f}: not a SHA-256 digest: {digest[:20]}")
                    block[digest.lower()] = name.strip() or f"Blocklist.{f.stem}"
        names = [r.name for r in rules]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise RuleError(f"duplicate rule names: {sorted(dupes)}")
        return cls(rules, block)

    def scan(self, path: str, data: bytes, size: int, sha256: str | None = None) -> list[Hit]:
        hits = []
        if sha256 and sha256 in self.blocklist:
            hits.append(Hit(self.blocklist[sha256], "malicious", "file on the blocklist", [sha256]))
        for r in self.rules:
            if r.applies(path, data, size):
                ev = r.match(data)
                if ev:
                    hits.append(Hit(r.name, r.severity, r.description, ev[:5]))
        return hits


def sha256_file(path: str | Path | None = None, fd: int | None = None, limit: int = 512 * 1024 * 1024) -> str | None:
    """SHA-256 of a whole file (by path or open descriptor); None if unreadable or over the limit."""
    import os
    h = hashlib.sha256()
    try:
        if fd is not None:
            off = 0
            while True:
                chunk = os.pread(fd, 1 << 20, off)
                if not chunk:
                    break
                h.update(chunk)
                off += len(chunk)
                if off > limit:
                    return None
        else:
            with open(path, "rb") as f:
                n = 0
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                    n += len(chunk)
                    if n > limit:
                        return None
    except OSError:
        return None
    return h.hexdigest()
