"""Country hopping: every x minutes (the user's number), move to the next country on their list."""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field


@dataclass
class Hopper:
    minutes: float
    countries: list[str]               # the user's checklist (Riseup: cities; Tor: country codes; WireGuard: config ids)
    order: str = "cycle"               # cycle | random
    clock: callable = time.monotonic
    rng: random.Random = field(default_factory=random.Random)
    next_at: float = 0.0

    def __post_init__(self):
        if self.minutes < 1:
            raise ValueError("hop at most once a minute")
        if len(self.countries) < 2:
            raise ValueError("pick at least two places to hop between")
        self.next_at = self.clock() + self.minutes * 60

    def due(self) -> bool:
        return self.clock() >= self.next_at

    def seconds_left(self) -> int:
        return max(0, math.ceil(self.next_at - self.clock()))

    def next(self, current: str | None) -> str:
        """The next place, never the current one; restarts the timer."""
        self.next_at = self.clock() + self.minutes * 60
        if self.order == "random":
            return self.rng.choice([c for c in self.countries if c != current] or self.countries)
        if current in self.countries:
            return self.countries[(self.countries.index(current) + 1) % len(self.countries)]
        return self.countries[0]
