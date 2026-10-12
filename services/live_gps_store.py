from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("live_gps_store")

TELEMETRY_FRESHNESS_MINUTES = 5

# In-memory only. Never persisted, never read from or written to Postgres.
# Keyed by normalized plate_no. This is the single source of truth for live
# vehicle telemetry; the vehicles table only stores static data.
_live: dict[str, dict] = {}


def normalize_gps_time(value) -> str | None:
    """Cartrack's device event timestamp ('2026-10-01 04:59:49+08') as an ISO-8601 UTC string, or None
    when it is missing or unparseable. This is the DEVICE's clock for the fix, unlike last_updated
    (the server's poll time)."""
    if not value:
        return None
    from services.fleet_health.daily import parse_ts

    parsed = parse_ts(value)
    return parsed.astimezone(timezone.utc).isoformat() if parsed is not None else None


def _fix_key(entry: dict | None) -> tuple | None:
    if entry is None:
        return None
    return (entry["lat"], entry["lng"], entry["heading"], entry["speed_kph"], entry["ignition_on"], entry["gps_time"])


def upsert_with_change(plate: str, *, lat: float, lng: float, heading: float, speed_kph: float,
                       fuel_pct: float | None, ignition_on: bool | None, address: str | None = None,
                       event_time=None) -> tuple[dict, bool]:
    """Store the latest fix. Returns (entry, changed): changed is False when the position, motion and
    device timestamp are identical to what was already stored, so the caller can skip a pointless
    broadcast. last_updated (the server's poll time) is refreshed either way."""
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
        "gps_time": normalize_gps_time(event_time),
        "last_updated": now,
    }
    changed = _fix_key(_live.get(plate)) != _fix_key(entry)
    _live[plate] = entry
    return entry, changed


def upsert(plate: str, *, lat: float, lng: float, heading: float, speed_kph: float,
           fuel_pct: float | None, ignition_on: bool | None, address: str | None = None,
           event_time=None) -> dict:
    return upsert_with_change(plate, lat=lat, lng=lng, heading=heading, speed_kph=speed_kph, fuel_pct=fuel_pct,
                              ignition_on=ignition_on, address=address, event_time=event_time)[0]


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
