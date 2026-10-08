"""In-memory status sampler: one /rest/vehicles/status call every 10 minutes for ALL vehicles.

Samples (odometer, ignition, speed, fuel %, vext, timestamp) are kept in memory for SAMPLE_RETENTION_DAYS and feed
the fuel and battery calculations of the nightly job. A restart empties them: the affected day then shows
data_quality.partial_day = true (see daily.coverage), never invented values.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from services import cartrack_client
from services.fleet_health import config
from services.fleet_health.daily import parse_ts
from services.fleet_health.matching import normalize_plate

logger = logging.getLogger("fleet_health.sampler")

_lock = threading.Lock()
_samples: dict[str, deque] = defaultdict(lambda: deque(maxlen=config.SAMPLE_RETENTION_DAYS * 24 * 6 + 12))
_started_at: datetime = datetime.now(timezone.utc)
_last_run: dict = {"at": None, "ok": None, "vehicles": 0, "error": None}


def parse_status(row: dict, captured_at: datetime | None = None) -> dict | None:
    """One /rest/vehicles/status entry -> a sample. Only fields verified on this fleet (no rpm/temp/clock/driver).
    `ts` is the CAPTURE time (a parked truck re-reports the same event_ts for hours, so event time cannot
    measure sampler coverage); the device's own event time is kept as `event_ts`."""
    event_ts = parse_ts(row.get("event_ts"))
    plate = normalize_plate(row.get("registration"))
    if not plate or event_ts is None:
        return None
    ts = (captured_at or datetime.now(timezone.utc)).astimezone(config.MANILA)
    fuel = row.get("fuel") or {}
    vext = row.get("vext")
    try:
        vext = float(vext) if vext not in (None, "") else None
    except (TypeError, ValueError):
        vext = None
    odometer = row.get("odometer")
    return {
        "plate_key": plate,
        "ts": ts,
        "event_ts": event_ts,
        "odometer_m": float(odometer) if odometer not in (None, "") else None,
        "ignition": bool(row.get("ignition")),
        "idling": bool(row.get("idling")),
        "speed": float(row.get("speed") or 0),
        "fuel_pct": float(fuel["precentage_left"]) if fuel.get("precentage_left") not in (None, "") else None,  # (sic) Cartrack's spelling
        "vext": vext,
    }


def record(sample: dict) -> bool:
    with _lock:
        bucket = _samples[sample["plate_key"]]
        bucket.append(sample)
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.SAMPLE_RETENTION_DAYS)
        while bucket and bucket[0]["ts"] < cutoff:
            bucket.popleft()
        return True


def samples_for(plate_key: str) -> list[dict]:
    with _lock:
        return list(_samples.get(plate_key, ()))


def reset() -> None:
    global _started_at
    with _lock:
        _samples.clear()
        _started_at = datetime.now(timezone.utc)


def started_at() -> datetime:
    return _started_at


def last_run() -> dict:
    return dict(_last_run)


def health() -> dict:
    """Small, non-sensitive summary for /health: has the sampler run, and how many samples are in memory."""
    with _lock:
        stored = sum(len(bucket) for bucket in _samples.values())
        vehicles = len(_samples)
    return {**_last_run, "samples_in_memory": stored, "vehicles_sampled": vehicles}


async def sample_once(counter=None) -> int:
    """ONE Cartrack call. Never raises (the scheduler must keep running); failures are logged and shown in last_run()."""
    try:
        payload = await cartrack_client.get_json("/rest/vehicles/status", counter=counter)
        rows = payload.get("data", []) if isinstance(payload, dict) else payload
        stored = 0
        for row in rows or []:
            sample = parse_status(row)
            if sample and record(sample):
                stored += 1
        _last_run.update(at=datetime.now(timezone.utc).isoformat(), ok=True, vehicles=len(rows or []), error=None)
        return stored
    except Exception as exc:  # noqa: BLE001 - a failed sample only leaves a gap that daily.coverage reports
        logger.warning("[FLEET_HEALTH] status sample failed: %s", exc)
        _last_run.update(at=datetime.now(timezone.utc).isoformat(), ok=False, error=str(exc)[:200])
        return 0
