"""Negative Selection (Artificial Immune System): detectors for "non-self".

In the thymus, T cells that react to the body's own cells are destroyed; the survivors react only to
what is foreign. Here "self" is the feature vectors (phagos.features) of files known to be fine on
this computer: package-owned programs, and the user's own files when Phagos first runs. Detectors
are random points kept only if they lie outside every self sample's radius (real-valued
V-detectors, Ji and Dasgupta 2004): each detector's radius reaches up to, never into, self. So a
file like the ones this computer already has never matches, and a file unlike all of them can.

Generation stops when this many candidates in a row land inside existing detectors, an estimate
that about `coverage` of the non-self space is covered.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass

from ..features import distance


@dataclass
class Detector:
    center: list[float]
    radius: float
    memory: str = ""          # name of the threat it was matured on (Clonal Selection), "" if naive

    def to_json(self) -> dict:
        return {"c": [round(x, 4) for x in self.center], "r": round(self.radius, 4), "m": self.memory}

    @classmethod
    def from_json(cls, d: dict) -> "Detector":
        return cls(d["c"], d["r"], d.get("m", ""))


class NegativeSelection:
    def __init__(self, dim: int, self_radius: float = 0.15, coverage: float = 0.99,
                 max_detectors: int = 1500, seed: int | None = None):
        self.dim, self.self_radius, self.coverage, self.max_detectors = dim, self_radius, coverage, max_detectors
        self.rng = random.Random(seed)
        self.self_points: list[list[float]] = []
        self.detectors: list[Detector] = []

    # ------------------------------------------------------------------ self
    @staticmethod
    def _key(v: list[float]) -> tuple:
        return tuple(round(x * 50) for x in v)      # 0.02 grid: near-identical files count once

    def add_self(self, points: list[list[float]]) -> int:
        seen = {self._key(p) for p in self.self_points}
        added = 0
        for p in points:
            k = self._key(p)
            if k not in seen and len(p) == self.dim:
                seen.add(k)
                self.self_points.append(list(p))
                added += 1
        return added

    def nearest_self(self, v: list[float]) -> float:
        return min((distance(v, s) for s in self.self_points), default=math.inf)

    # ------------------------------------------------------------------ detectors
    def train(self) -> int:
        """Generate naive detectors until about `coverage` of non-self is covered. Returns how many."""
        need = max(10, int(round(1 / max(1e-6, 1 - self.coverage))))
        in_a_row = 0
        tries = 0
        while len(self.detectors) < self.max_detectors and in_a_row < need and tries < self.max_detectors * 50:
            tries += 1
            x = [self.rng.random() for _ in range(self.dim)]
            if any(distance(x, d.center) < d.radius for d in self.detectors):
                in_a_row += 1
                continue
            in_a_row = 0
            r = self.nearest_self(x) - self.self_radius
            if r > 0:
                self.detectors.append(Detector(x, r))
        return len(self.detectors)

    def matches(self, v: list[float]) -> list[tuple[Detector, float]]:
        """Detectors containing v, with how deep inside (0 at the edge, 1 at the centre)."""
        out = []
        for d in self.detectors:
            dist = distance(v, d.center)
            if dist < d.radius:
                out.append((d, 1 - dist / d.radius))
        return out

    def score(self, v: list[float]) -> tuple[float, str]:
        """Anomaly score in [0, 1] (0 = self-like) and the memory name of the best detector, if any."""
        best, name = 0.0, ""
        for d, depth in self.matches(v):
            # a memory detector (matured on a real threat) counts a little more than a naive one
            s = min(1.0, depth * (1.25 if d.memory else 1.0))
            if s > best:
                best, name = s, d.memory
        return best, name

    def tolerate(self, v: list[float]) -> int:
        """The user says this is fine: it becomes self, and detectors that cover it are removed."""
        before = len(self.detectors)
        self.detectors = [d for d in self.detectors if distance(v, d.center) >= d.radius]
        self.add_self([v])
        # remaining detectors may now reach into the new self sample: shrink them
        for d in self.detectors:
            d.radius = min(d.radius, distance(d.center, v) - self.self_radius)
        self.detectors = [d for d in self.detectors if d.radius > 0]
        return before - len(self.detectors)

    # ------------------------------------------------------------------ persistence
    def to_json(self) -> dict:
        return {"dim": self.dim, "self_radius": self.self_radius, "coverage": self.coverage,
                "self": [[round(x, 4) for x in p] for p in self.self_points],
                "detectors": [d.to_json() for d in self.detectors]}

    @classmethod
    def from_json(cls, d: dict, seed: int | None = None) -> "NegativeSelection":
        n = cls(d["dim"], d["self_radius"], d["coverage"], seed=seed)
        n.self_points = d["self"]
        n.detectors = [Detector.from_json(x) for x in d["detectors"]]
        return n

    def save(self, path) -> None:
        from pathlib import Path
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(self.to_json()))
        tmp.replace(path)
