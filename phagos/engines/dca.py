"""Dendritic Cell Algorithm (Danger Theory; Greensmith, Aickelin and Cayzer 2005; deterministic DCA,
Greensmith and Aickelin 2008).

Danger Theory (Matzinger) says the immune system reacts to damage, not merely to the unfamiliar.
Dendritic cells sample antigens (here: processes) together with the signals around them:
  PAMP    sure signs of attack: a signature hit, a decoy file written, running from a deleted file
  danger  signs of damage: mass rewrites of user files with random-looking (encrypted) data, write
          bursts, many new outbound connections, sustained CPU
  safe    signs of normal life: a program installed by a package, behaviour matching its own past
Each cell adds up its inputs; when its total (csm) passes the cell's own migration threshold it
"migrates" and presents everything it collected, as mature (dangerous context, k > 0) or
semi-mature (safe context). A process's MCAV (mature context antigen value) is the share of its
presentations made in a dangerous context. Cells have different thresholds, so the verdict is an
average over short and long views of the process's recent life.

Signals here belong to each process, so each process gets its own small population of cells (the
classic DCA shares one population and one signal stream between all antigens; shared cells would let
one busy, safe process dilute another's danger). PAMP counts double in the danger input.
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class Cell:
    threshold: float
    csm: float = 0.0
    k: float = 0.0
    antigens: Counter = field(default_factory=Counter)


class DendriticCells:
    def __init__(self, n_cells: int = 12, thresholds: tuple[float, float] = (8.0, 30.0), per_antigen: int = 3,
                 anomaly: float = 0.65, min_presented: int = 6, window: int = 40, seed: int | None = None):
        self.rng = random.Random(seed)
        self.n_cells, self.thresholds = n_cells, thresholds
        self.per_antigen, self.anomaly, self.min_presented, self.window = per_antigen, anomaly, min_presented, window
        self.populations: dict[str, list[Cell]] = {}
        self.history: dict[str, list[int]] = {}   # antigen -> recent presentations (1 mature, 0 semi)

    def _cells(self, antigen: str) -> list[Cell]:
        cells = self.populations.get(antigen)
        if cells is None:
            lo, hi = self.thresholds
            cells = [Cell(lo + (hi - lo) * i / max(1, self.n_cells - 1)) for i in range(self.n_cells)]
            self.populations[antigen] = cells
        return cells

    def sample(self, antigen: str, pamp: float = 0.0, danger: float = 0.0, safe: float = 0.0) -> None:
        """One observation of a process with its signals (each about 0..10)."""
        d = danger + 2.0 * pamp
        cells = self._cells(antigen)
        for c in self.rng.sample(cells, min(self.per_antigen, len(cells))):
            c.antigens[antigen] += 1
            c.csm += d + safe
            c.k += d - 2.0 * safe
            if c.csm >= c.threshold:
                self._migrate(c)

    def _migrate(self, c: Cell) -> None:
        context = 1 if c.k > 0 else 0
        for a, n in c.antigens.items():
            h = self.history.setdefault(a, [])
            h.extend([context] * n)
            del h[:-self.window]
        c.csm, c.k = 0.0, 0.0
        c.antigens.clear()

    def mcav(self, antigen: str) -> tuple[float, int]:
        h = self.history.get(antigen, [])
        return (sum(h) / len(h) if h else 0.0), len(h)

    def anomalous(self, antigen: str) -> bool:
        v, n = self.mcav(antigen)
        return n >= self.min_presented and v >= self.anomaly

    def forget(self, antigen: str) -> None:
        """A process ended or was decided on: drop its cells and history."""
        self.history.pop(antigen, None)
        self.populations.pop(antigen, None)
