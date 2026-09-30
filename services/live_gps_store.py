from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("live_gps_store")

TELEMETRY_FRESHNESS_MINUTES = 5

# In-memory only. Never persisted, never read from or written to Postgres.
# Keyed by normalized plate_no. This is the single source of truth for live
# vehicle telemetry; the vehicles table only stores static data.
_live: dict[str, dict] = {}


def upsert(plate: str, *, lat: float, lng: float, heading: float, speed_kph: float,
           fuel_pct: float | None, ignition_on: bool | None, address: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    status = "Active" if (speed_kph or 0) > 0 or ignition_on else "Idle"
    entry = {
        "plate_no": plate,
        "lat": lat,
        "lng": lng,
        "heading": heading,
        "speed_kph": speed_kph,
        "fuel_pct": fuel_pct,
        "ignition_on": ignition_on,
        "status": status,
        "zone": None,
        "address": address,
        "last_updated": now,
    }
    _live[plate] = entry
    return entry


def get(plate: str) -> dict | None:
    entry = _live.get(plate)
    if entry is None:
        return None
    age_seconds = (datetime.now(timezone.utc) - entry["last_updated"]).total_seconds()
    if age_seconds > TELEMETRY_FRESHNESS_MINUTES * 60 and entry["status"] != "No signal":
        entry = {**entry, "status": "No signal"}
        _live[plate] = entry
    return entry


def all_entries() -> dict[str, dict]:
    return dict(_live)
