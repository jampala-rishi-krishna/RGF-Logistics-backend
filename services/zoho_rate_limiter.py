"""Organization-level admission control for actual Zoho Inventory HTTP attempts.

Two independent controls, deliberately kept separate:
  * concurrency - how many attempts may be in flight at once (bounded semaphore)
  * pacing      - how often new attempts may start (GCRA / leaky bucket with a burst)

One limiter exists per Zoho organization (one per process today), shared by every feature
(Fleet, Dispatch, Reports, Load Planning, schedulers). Only the owner of a coalesced GET
and every retry attempt pass through here; waiters and cache hits never do.
No lock is held while sleeping or during network I/O. See docs/ZOHO_ACQUISITION.md.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass

logger = logging.getLogger("zoho")

# Defaults (override via env, all optional):
#   ZOHO_API_RATE_PER_MINUTE   documented Zoho Inventory ceiling per organization      (100)
#   ZOHO_RATE_SAFETY_PERCENT   share of that ceiling IntelliFleet may use sustained    (80)
#   ZOHO_RATE_BURST            attempts allowed back-to-back before pacing begins       (10)
#   ZOHO_MAX_CONCURRENCY       simultaneous in-flight Zoho HTTP attempts                (5)
# Sustained 80/min + burst 10 keeps any 60 s window <= ~90, under the 100/min ceiling.
DEFAULTS = {
    "ZOHO_API_RATE_PER_MINUTE": 100.0,
    "ZOHO_RATE_SAFETY_PERCENT": 80.0,
    "ZOHO_RATE_BURST": 10,
    "ZOHO_MAX_CONCURRENCY": 5,
}


def _env_number(name: str, cast, minimum):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return DEFAULTS[name]
    try:
        value = cast(raw)
        if value < minimum:
            raise ValueError
        return value
    except ValueError:
        logger.warning("[ZOHO_LIMIT] invalid %s, using default %s", name, DEFAULTS[name])
        return DEFAULTS[name]


def config_from_env() -> dict:
    return {
        "rate_per_minute": _env_number("ZOHO_API_RATE_PER_MINUTE", float, 1.0),
        "safety_percent": min(100.0, _env_number("ZOHO_RATE_SAFETY_PERCENT", float, 1.0)),
        "burst": _env_number("ZOHO_RATE_BURST", int, 1),
        "max_concurrency": _env_number("ZOHO_MAX_CONCURRENCY", int, 1),
    }


# ---- metrics ---------------------------------------------------------------------------
class _Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self._counts = Counter()
            self._rate_wait_ms = 0.0
            self._concurrency_wait_ms = 0.0
            self._max_in_flight = 0

    def record(self, name: str, n: int = 1):
        with self._lock:
            self._counts[name] += n

    def observe(self, rate_wait_ms: float, concurrency_wait_ms: float, in_flight: int):
        with self._lock:
            self._counts["http_attempts"] += 1
            self._rate_wait_ms += rate_wait_ms
            self._concurrency_wait_ms += concurrency_wait_ms
            self._max_in_flight = max(self._max_in_flight, in_flight)

    def snapshot(self) -> dict:
        with self._lock:
            attempts = self._counts["http_attempts"]
            return {
                "logical_requests": self._counts["logical_request"],
                "http_attempts": attempts,
                "successful_attempts": self._counts["http_success"],
                "http_429": self._counts["http_429"],
                "retry_attempts": self._counts["http_retry"],
                "coalesced_waiters": self._counts["coalesced_waiter"],
                "cache_hits": self._counts["cache_hit"],
                "cache_misses": self._counts["cache_miss"],
                "avg_rate_wait_ms": self._rate_wait_ms / attempts if attempts else 0.0,
                "avg_concurrency_wait_ms": self._concurrency_wait_ms / attempts if attempts else 0.0,
                "max_concurrency": self._max_in_flight,
            }


metrics = _Metrics()


# ---- limiter ---------------------------------------------------------------------------
@dataclass
class Ticket:
    rate_wait_ms: int = 0
    concurrency_wait_ms: int = 0


class ZohoRateLimiter:
    def __init__(self, rate_per_minute: float, safety_percent: float, burst: int, max_concurrency: int,
                 clock=time.monotonic, sleep=time.sleep):
        self.interval = 60.0 / (rate_per_minute * safety_percent / 100.0)
        self.burst = burst
        self.max_concurrency = max_concurrency
        self._clock, self._sleep = clock, sleep
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._lock = threading.Lock()  # guards the fields below; never held while sleeping
        self._tat = 0.0  # GCRA theoretical arrival time
        self._cooldown_until = 0.0
        self.in_flight = 0

    def _reserve(self) -> float:
        """Reserve the next start time and return how long to wait for it."""
        with self._lock:
            now = self._clock()
            new_tat = max(self._tat, now, self._cooldown_until) + self.interval
            allow_at = max(new_tat - self.burst * self.interval, self._cooldown_until)
            self._tat = new_tat
            return max(0.0, allow_at - now)

    def penalize(self, seconds: float) -> None:
        """Org-wide backpressure: after a 429 no new attempt starts until the cooldown ends."""
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self._clock() + seconds)

    @contextmanager
    def admit(self):
        """Wrap exactly one HTTP attempt. Capacity is always released, including on error."""
        t0 = self._clock()
        self._slots.acquire()
        try:
            t1 = self._clock()
            wait = self._reserve()
            if wait > 0:
                self._sleep(wait)
            ticket = Ticket(rate_wait_ms=int(wait * 1000), concurrency_wait_ms=int((t1 - t0) * 1000))
            with self._lock:
                self.in_flight += 1
                in_flight = self.in_flight
            metrics.observe(ticket.rate_wait_ms, ticket.concurrency_wait_ms, in_flight)
            try:
                yield ticket
            finally:
                with self._lock:
                    self.in_flight -= 1
        finally:
            self._slots.release()


_registry: dict[str, ZohoRateLimiter] = {}
_registry_lock = threading.Lock()
_overrides: dict = {}


def limiter_for(org_id: str) -> ZohoRateLimiter:
    """The single shared limiter for an organization (credentials/orgs never share a budget)."""
    with _registry_lock:
        limiter = _registry.get(org_id)
        if limiter is None:
            limiter = _registry[org_id] = ZohoRateLimiter(**{**config_from_env(), **_overrides})
        return limiter


def reset(**overrides) -> None:
    """Drop all limiters and metrics (tests / config reload). Overrides replace env config."""
    with _registry_lock:
        _registry.clear()
        _overrides.clear()
        _overrides.update(overrides)
    metrics.reset()
