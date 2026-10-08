"""One shared Cartrack call budget for the whole backend: at most CARTRACK_MAX_CALLS_PER_MINUTE (20)
in any rolling 60 s window. Fleet Health calls (sampler, nightly job, backfill) WAIT for capacity;
the existing live-GPS poller only RECORDS its calls (note) so it is never delayed, but it still
counts against the shared budget the waiting jobs see.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import deque

DEFAULT_MAX_CALLS_PER_MINUTE = 20
RETRY_DELAYS_SECONDS = (1, 2, 4, 8)  # on 429 / 5xx / timeout, then give up


class CartrackCallCounter:
    """Counts the Cartrack HTTP calls one job makes (for the 'calls per run' report)."""

    def __init__(self) -> None:
        self.calls = 0
        self.by_path: dict[str, int] = {}

    def add(self, path: str) -> None:
        self.calls += 1
        key = "/".join(path.split("/")[:3])  # /rest/trips/NFX5791 -> /rest/trips
        self.by_path[key] = self.by_path.get(key, 0) + 1


class SharedRateLimiter:
    def __init__(self, max_calls: int | None = None, window_seconds: float = 60.0, clock=time.monotonic, sleep=asyncio.sleep) -> None:
        self.max_calls = max_calls or int(os.environ.get("CARTRACK_MAX_CALLS_PER_MINUTE", DEFAULT_MAX_CALLS_PER_MINUTE))
        self.window = window_seconds
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._mutex = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= self.window:
            self._calls.popleft()

    async def acquire(self) -> None:
        """Wait until a call fits in the rolling window, then reserve it."""
        while True:
            with self._mutex:
                now = self._clock()
                self._prune(now)
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self._calls[0] + self.window - now
            await self._sleep(max(wait, 0.05))

    def note(self) -> None:
        """Record a call made by something that must not wait (the live GPS poller)."""
        with self._mutex:
            now = self._clock()
            self._prune(now)
            self._calls.append(now)

    def in_window(self) -> int:
        with self._mutex:
            self._prune(self._clock())
            return len(self._calls)


shared = SharedRateLimiter()
