"""In-memory login throttle (approved by Rishi 2026-10-12: single worker, single instance, no table).

5 failed attempts inside 15 minutes lock a key; keys are the submitted identifier (whether or not such an
account exists, so a lock never reveals that an account does) and the client IP. A successful login clears the
account key only. Counters reset on restart and are not shared between workers - acceptable for the current
single-instance deployment; revisit if the API ever runs on more than one worker.
"""
from __future__ import annotations

import threading
import time

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60
_MAX_KEYS = 20_000

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}


def _recent(key: str, now: float) -> list[float]:
    stamps = [t for t in _failures.get(key, []) if now - t < WINDOW_SECONDS]
    if stamps:
        _failures[key] = stamps
    else:
        _failures.pop(key, None)
    return stamps


def is_blocked(*keys: str) -> bool:
    now = time.monotonic()
    with _lock:
        return any(len(_recent(key, now)) >= MAX_FAILURES for key in keys if key)


def retry_after_seconds(*keys: str) -> int:
    now = time.monotonic()
    with _lock:
        waits = [WINDOW_SECONDS - (now - _recent(key, now)[0]) for key in keys if key and len(_recent(key, now)) >= MAX_FAILURES]
    return max(1, int(max(waits))) if waits else 0


def record_failure(*keys: str) -> None:
    now = time.monotonic()
    with _lock:
        if len(_failures) > _MAX_KEYS:  # bound memory under a spray attack: drop everything that has aged out
            for stale in [k for k in list(_failures) if not _recent(k, now)]:
                _failures.pop(stale, None)
        for key in keys:
            if key:
                _failures.setdefault(key, []).append(now)


def clear(*keys: str) -> None:
    with _lock:
        for key in keys:
            _failures.pop(key, None)


def reset_all() -> None:
    """Test hook."""
    with _lock:
        _failures.clear()
