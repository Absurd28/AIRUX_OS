"""Clonal Selection (CLONALG, de Castro and Von Zuben 2002) applied to Negative Selection detectors.

When a B cell's antibody binds an invader, the cell multiplies; its clones mutate, and the ones
that bind better survive as memory cells, so the next infection by a similar invader is met faster.
Here, when a threat is confirmed (a signature hit, or the user chooses "quarantine"):
  1. the detectors nearest the threat's feature vector are selected;
  2. each is cloned, more clones for closer detectors;
  3. clones move toward the threat and mutate, less for higher affinity;
  4. every clone passes negative selection again (it must not reach into self);
  5. the clone that covers the threat with the widest margin becomes a memory detector, named
     after the threat.
Variants of that threat (similar vectors) then fall inside the memory detector.
"""
from __future__ import annotations

import math
import random

from ..features import distance
from .nsa import Detector, NegativeSelection


def affinity(d: Detector, v: list[float]) -> float:
    """Higher when the detector is closer to (or deeper around) the antigen; in (0, 1]."""
    return 1.0 / (1.0 + max(0.0, distance(d.center, v) - d.radius) + distance(d.center, v) * 0.1)


def mature(nsa: NegativeSelection, antigen: list[float], name: str, select: int = 5, clones: int = 10,
           rho: float = 3.0, rng: random.Random | None = None) -> Detector | None:
    """Clone and mutate detectors toward a confirmed threat; add the best clone as a memory detector.

    Returns the new memory detector, or None if the threat sits inside self (no detector can cover
    it without also covering files known to be fine, so it is left to the signatures).
    """
    rng = rng or random.Random()
    if nsa.nearest_self(antigen) <= nsa.self_radius:
        return None
    pool = sorted(nsa.detectors, key=lambda d: -affinity(d, antigen))[:select]
    # a seed at the antigen itself, for the case where no detector is near yet
    pool.append(Detector(list(antigen), 0.0))
    best: tuple[float, Detector] | None = None
    for rank, d in enumerate(pool, 1):
        aff = affinity(d, antigen)
        n_clones = max(1, round(clones / rank))
        step = math.exp(-rho * aff)                 # mutation size: small for good detectors
        for _ in range(n_clones):
            t = rng.uniform(0.3, 1.0)
            c = [min(1.0, max(0.0, x + (a - x) * t + rng.gauss(0, step * 0.15))) for x, a in zip(d.center, antigen)]
            r = nsa.nearest_self(c) - nsa.self_radius            # negative selection, again
            if r <= 0:
                continue
            margin = r - distance(c, antigen)
            if margin > 0 and (best is None or margin > best[0]):
                best = (margin, Detector(c, r, name))
    if best is None:
        return None
    nsa.detectors.append(best[1])
    return best[1]
