"""Static heuristics: what an executable or script looks like, scored with plain rules.

Weak alone (each trait has innocent uses), useful together; the scanner combines this score with
Negative Selection and the signature rules.
"""
from __future__ import annotations

from ..features import FileInfo


def score(info: FileInfo) -> tuple[float, list[str]]:
    t = info.traits
    executable = t["elf"] or t["script"] or bool(info.mode & 0o111)
    if not executable:
        return 0.0, []
    s, why = 0.0, []
    if t["risky_place"] and (info.mode & 0o111):
        s += 0.3; why.append("executable in a temporary folder")
    if t["packed"]:
        s += 0.3; why.append("packed or encrypted program")
    if t["elf"] and t["static"] and t["net_strings"] >= 2:
        s += 0.2; why.append("self-contained program with network addresses inside")
    n = len(t["suspicious"])
    if n >= 4:
        s += 0.5; why.append(f"{n} suspicious commands or strings")
    elif n >= 2:
        s += 0.3; why.append(f"{n} suspicious commands or strings")
    elif n == 1 and t["risky_place"]:
        s += 0.2; why.append("a suspicious command in a temporary folder")
    return min(1.0, s), why
