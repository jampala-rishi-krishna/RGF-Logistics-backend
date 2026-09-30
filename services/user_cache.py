from __future__ import annotations

import threading
import time

TTL_SECONDS = 600  # 10 min

_lock = threading.Lock()
_cache: dict[int, tuple[float, dict]] = {}


def get(user_id: int) -> dict | None:
    with _lock:
        entry = _cache.get(user_id)
        if entry is None:
            return None
        expires_at, user = entry
        if time.monotonic() > expires_at:
            del _cache[user_id]
            return None
        return user


def put(user_id: int, user: dict) -> None:
    with _lock:
        _cache[user_id] = (time.monotonic() + TTL_SECONDS, user)


def invalidate(user_id: int) -> None:
    """Call on password change, role change, or user disable/enable - anything that
    changes what a JWT for this user_id should be allowed to do."""
    with _lock:
        _cache.pop(user_id, None)
