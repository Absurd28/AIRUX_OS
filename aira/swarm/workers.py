"""Worker pool for non-model jobs (file searches, page fetches, scripts).

A job kind has several approaches (functions). The pool runs the parts of a task in parallel;
for each part it tries approaches in pheromone order and stops at the first success, depositing
a score for every attempt. Jobs that wait on subprocesses or the network release the GIL, so
threads give real parallelism for them.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .pheromones import PheromoneBoard


@dataclass
class PartResult:
    part: Any
    ok: bool
    value: Any = None
    approach: str = ""
    error: str = ""
    seconds: float = 0.0
    tried: list[str] = field(default_factory=list)


class WorkerPool:
    def __init__(self, board: PheromoneBoard, workers: int | None = None,
                 limit: Callable[[], int] | None = None):
        self.board = board
        self.workers = workers or min(8, os.cpu_count() or 2)
        self.limit = limit  # load controller: current number of allowed parallel workers

    def size(self) -> int:
        return max(1, min(self.workers, self.limit() if self.limit else self.workers))

    def _run_part(self, kind: str, approaches: dict[str, Callable], part) -> PartResult:
        res = PartResult(part, False)
        for name in self.board.ranked(kind, list(approaches)):
            t0 = time.monotonic()
            res.tried.append(name)
            try:
                value = approaches[name](part)
            except Exception as exc:
                self.board.deposit(kind, name, False, time.monotonic() - t0)
                res.error = f"{name}: {exc}"
                continue
            dt_s = time.monotonic() - t0
            self.board.deposit(kind, name, True, dt_s)
            return PartResult(part, True, value, name, "", dt_s, res.tried)
        return res

    def map(self, kind: str, approaches: dict[str, Callable], parts: list, workers: int | None = None
            ) -> list[PartResult]:
        n = max(1, min(workers or self.size(), len(parts) or 1))
        if n == 1:
            return [self._run_part(kind, approaches, p) for p in parts]
        with ThreadPoolExecutor(max_workers=n) as pool:
            return list(pool.map(lambda p: self._run_part(kind, approaches, p), parts))
