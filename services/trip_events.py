"""Shared plumbing for every trip action (crew actions in services/trip_workflow.py, officer actions in
services/handover.py): error shape, actor, phone timestamps + GPS cleaning, the append-only event log and the
idempotency ledger.

Rules every action follows:
  * ONE write per action: the state change plus its trip_events row, in one transaction, committed once.
  * Idempotency: the client sends a UUID `idempotency_key`. A key seen before returns the stored `result` of the first
    execution (marked replayed) and writes nothing. Two racing requests with the same key: the unique index makes the
    second one fail; `commit_or_replay` turns that into the stored result.
  * Times: the phone's `client_ts` is the action time; `received_at` is the server clock. Both are stored.
  * GPS: {"lat","lng","accuracy_m","reason"}; lat/lng null + a reason when the phone denied / could not locate.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.trip import TripEvent

MAX_CLIENT_PAST = timedelta(days=3)  # a phone queued offline for longer than this is treated as having a wrong clock
MAX_CLIENT_FUTURE = timedelta(minutes=10)
FAR_THRESHOLD_M = 500.0  # check-in further than this from the address is flagged (a warning, never a block)

EVENT_TYPES = ("acknowledged", "trip_started", "checked_in", "checked_out", "trip_ended", "handover_submitted", "officer_acknowledged", "invoice_validated", "invoice_resolved", "trip_closed")


def fail(status: int, message: str, code: str, errors: list[dict] | None = None):
    """Uniform error: {"detail": {"message", "code", "errors"?}}. Codes: validation, out_of_order, forbidden, not_found, conflict, storage_disabled."""
    detail: dict[str, Any] = {"message": message, "code": code}
    if errors:
        detail["errors"] = errors
    raise HTTPException(status, detail=detail)


@dataclass
class Actor:
    user_id: int
    role: str  # driver | helper | logistics_officer | admin
    staff_id: int | None
    name: str

    def public(self) -> dict:
        return {"user_id": self.user_id, "name": self.name, "role": self.role}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_client_ts(value: str | datetime | None, received_at: datetime) -> tuple[datetime, bool]:
    """(action time, adjusted). Missing / unparsable / absurdly old or future client times fall back to the server time
    (adjusted=True) so one phone with a wrong clock cannot corrupt a timeline."""
    if value is None or value == "":
        return received_at, False
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return received_at, True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed < received_at - MAX_CLIENT_PAST or parsed > received_at + MAX_CLIENT_FUTURE:
        return received_at, True
    return parsed, False


def clean_gps(gps: dict | None, denied_reason: str | None = None) -> dict:
    """Always returns a dict, so "no GPS" is recorded explicitly with its reason."""
    if isinstance(gps, dict):
        try:
            lat, lng = float(gps.get("lat")), float(gps.get("lng"))
            if -90 <= lat <= 90 and -180 <= lng <= 180 and not (math.isnan(lat) or math.isnan(lng)):
                accuracy = gps.get("accuracy_m", gps.get("accuracy"))
                return {"lat": lat, "lng": lng, "accuracy_m": float(accuracy) if accuracy is not None else None, "reason": None}
        except (TypeError, ValueError):
            pass
        return {"lat": None, "lng": None, "accuracy_m": None, "reason": "invalid"}
    return {"lat": None, "lng": None, "accuracy_m": None, "reason": (denied_reason or "not_provided")}


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def distance_between(gps: dict | None, lat: float | None, lng: float | None) -> float | None:
    if not gps or gps.get("lat") is None or lat is None or lng is None:
        return None
    return round(haversine_m(gps["lat"], gps["lng"], lat, lng), 1)


def find_by_key(db: Session, key: str | None) -> TripEvent | None:
    if not key:
        return None
    return db.execute(select(TripEvent).where(TripEvent.idempotency_key == key)).scalar_one_or_none()


def replay_of(db: Session, key: str | None) -> dict | None:
    """The stored response of a previous execution with this key, marked replayed; None for a new key."""
    event = find_by_key(db, key)
    if event is None:
        return None
    return {**(event.result or {"ok": True}), "replayed": True}


def add_event(db: Session, *, trip_id: int | None, batch_id: str, event_type: str, actor: Actor, received_at: datetime, client_ts: datetime,
              gps: dict | None = None, so_id: str | None = None, key: str | None = None, payload: dict | None = None, result: dict | None = None) -> TripEvent:
    """Adds (does not commit) one audit row. `result` is what a replay of `key` will return."""
    event = TripEvent(trip_id=trip_id, batch_id=batch_id, event_type=event_type, so_id=so_id, user_id=actor.user_id, role=actor.role, staff_id=actor.staff_id,
                      client_ts=client_ts, received_at=received_at, gps=gps, idempotency_key=key or None, payload=payload, result=result)
    db.add(event)
    return event


def commit_or_replay(db: Session, key: str | None) -> dict | None:
    """Commit the transaction. If a concurrent request with the same key won the race, roll back and return ITS stored
    result (replayed); otherwise None."""
    try:
        db.commit()
        return None
    except IntegrityError:
        db.rollback()
        replay = replay_of(db, key)
        if replay is None:
            raise
        return replay


# ---- in-memory generation counter: lets read-heavy views (driver board, overview) re-query only after a write ----

_generation = 0
_generation_lock = threading.Lock()


def generation() -> int:
    return _generation


def bump() -> None:
    """Call after every committed trip / handover write."""
    global _generation
    with _generation_lock:
        _generation += 1
