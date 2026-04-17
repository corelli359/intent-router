from __future__ import annotations

import asyncio
from collections.abc import Callable
from threading import RLock

from admin_service.perf.models import PerfTestRunDetail, PerfTestRunNotFoundError


RunMutator = Callable[[PerfTestRunDetail], None]


class PerfTestRunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, PerfTestRunDetail] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    def create_run(self, run: PerfTestRunDetail) -> PerfTestRunDetail:
        with self._lock:
            self._runs[run.run_id] = run.model_copy(deep=True)
            return self._runs[run.run_id].model_copy(deep=True)

    def list_runs(self) -> list[PerfTestRunDetail]:
        with self._lock:
            ordered = sorted(self._runs.values(), key=lambda item: item.created_at, reverse=True)
            return [run.model_copy(deep=True) for run in ordered]

    def get_run(self, run_id: str) -> PerfTestRunDetail:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise PerfTestRunNotFoundError(f"perf test run not found: {run_id}")
            return run.model_copy(deep=True)

    def update_run(self, run_id: str, mutator: RunMutator) -> PerfTestRunDetail:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise PerfTestRunNotFoundError(f"perf test run not found: {run_id}")
            mutator(run)
            return run.model_copy(deep=True)

    def set_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        with self._lock:
            self._tasks[run_id] = task

    def get_task(self, run_id: str) -> asyncio.Task[None] | None:
        with self._lock:
            return self._tasks.get(run_id)

    def clear_task(self, run_id: str) -> None:
        with self._lock:
            self._tasks.pop(run_id, None)
