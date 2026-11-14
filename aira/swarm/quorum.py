"""Quorum: run several independent approaches and accept a result only if enough agree.

Used for the largest tier (build plan: "full swarm with quorum checks"). A result is accepted when
at least `need` approaches return the same normalised value; otherwise the disagreement is
reported, never resolved by guessing.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class QuorumResult:
    agreed: bool
    value: Any
    votes: dict[str, str]        # approach -> normalised value (or error)
    support: int


def _norm(v: Any) -> str:
    if isinstance(v, (list, tuple, set)):
        v = sorted(_norm(x) for x in v)
    return json.dumps(v, sort_keys=True, default=str)


def quorum(approaches: dict[str, Callable[[], Any]], need: int = 2) -> QuorumResult:
    with ThreadPoolExecutor(max_workers=len(approaches) or 1) as pool:
        futures = {name: pool.submit(fn) for name, fn in approaches.items()}
    votes, values = {}, {}
    for name, f in futures.items():
        try:
            v = f.result()
            key = _norm(v)
            votes[name] = key
            values.setdefault(key, v)
        except Exception as exc:
            votes[name] = f"error: {exc}"
    counts: dict[str, int] = {}
    for key in votes.values():
        if not key.startswith("error:"):
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return QuorumResult(False, None, votes, 0)
    best = max(counts, key=counts.get)
    return QuorumResult(counts[best] >= need, values[best] if counts[best] >= need else None, votes, counts[best])
