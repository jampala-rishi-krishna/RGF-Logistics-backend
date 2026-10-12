"""How often each device really reports a new GPS fix, measured from the polls we already make.

In memory only, no Cartrack calls of its own, nothing persisted. For every vehicle it remembers the last
device timestamp (Cartrack `event_ts`) and, each time that timestamp advances, the gap since the previous
one - kept separately for a moving truck (speed > 0) and a parked one. GET /api/admin/gps-cadence reads it.
Use it to decide CARTRACK_POLL_INTERVAL_SECONDS: polling faster than the median moving gap gains nothing.
"""
from __future__ import annotations

import statistics
import threading
from collections import deque
from datetime import datetime, timezone

SAMPLES_PER_BUCKET = 200
EVENT_TS_FIELD = "event_ts"  # the Cartrack /rest/vehicles/status field the timestamp comes from

_lock = threading.Lock()
_vehicles: dict[str, dict] = {}
_totals = {"polls": 0, "fixes_seen": 0, "broadcasts_sent": 0, "broadcasts_skipped_unchanged": 0}
_started_at = datetime.now(timezone.utc)


def _parse(gps_time_iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(gps_time_iso) if gps_time_iso else None
    except ValueError:
        return None


def note_poll() -> None:
    with _lock:
        _totals["polls"] += 1


def record(plate: str, gps_time_iso: str | None, speed_kph: float | None, *, changed: bool) -> None:
    """One vehicle seen in one poll. `changed` is whether the stored fix differed from the previous poll."""
    when = _parse(gps_time_iso)
    with _lock:
        _totals["fixes_seen"] += 1
        _totals["broadcasts_sent" if changed else "broadcasts_skipped_unchanged"] += 1
        v = _vehicles.setdefault(plate, {"last_gps": None, "polls": 0, "advances": 0,
                                         "moving": deque(maxlen=SAMPLES_PER_BUCKET), "parked": deque(maxlen=SAMPLES_PER_BUCKET)})
        v["polls"] += 1
        if when is None:
            return
        previous = v["last_gps"]
        if previous is not None and when > previous:
            v["advances"] += 1
            v["moving" if (speed_kph or 0) > 0 else "parked"].append((when - previous).total_seconds())
        if previous is None or when > previous:
            v["last_gps"] = when


def _summary(samples) -> dict:
    values = list(samples)
    if not values:
        return {"samples": 0, "median_s": None, "min_s": None, "max_s": None}
    return {"samples": len(values), "median_s": round(statistics.median(values), 1),
            "min_s": round(min(values), 1), "max_s": round(max(values), 1)}


def snapshot() -> dict:
    with _lock:
        per_vehicle = {
            plate: {"gps_time_field": EVENT_TS_FIELD, "polls": v["polls"], "timestamp_advances": v["advances"],
                    "last_gps_time": v["last_gps"].isoformat() if v["last_gps"] else None,
                    "moving_gap": _summary(v["moving"]), "parked_gap": _summary(v["parked"])}
            for plate, v in sorted(_vehicles.items())
        }
        moving = [g for v in _vehicles.values() for g in v["moving"]]
        return {"since": _started_at.isoformat(), "gps_time_field": EVENT_TS_FIELD, **_totals,
                "resolution_note": "A gap can never be smaller than the poll interval: fixes reported between two polls are "
                                   "collapsed into the newest one. A moving median equal to the poll interval means the device "
                                   "reports at least that often; poll faster to find out how much faster.",
                "all_vehicles_moving_gap": _summary(moving), "vehicles": per_vehicle}


def reset() -> None:
    """Test helper."""
    with _lock:
        _vehicles.clear()
        for key in _totals:
            _totals[key] = 0
