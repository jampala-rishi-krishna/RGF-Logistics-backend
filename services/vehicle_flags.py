"""The ONE entry point for every vehicle issue: manual, checklist, battery, fuel, overload, voice, email, whatsapp and
(later) the driver mobile app. Nothing else writes to vehicle_flags.

    report_issue(vehicle, source, severity, message, ref=None, reported_by=None, occurred_at=None, photo_ref=None, *, db=None)

* vehicle     - vehicles.id (int) or the plate ("NFX 5791", "nfx5791" ... are all normalised)
* source      - voice | email | whatsapp | checklist | battery | fuel | overload | manual | driver_app
* severity    - info | warning | critical
* ref         - call id / thread id / SO number / checklist id (a reference only)
* photo_ref   - a link/reference to a photo (never file content: nothing is stored in Neon)
* reported_by - free text identity of the reporter (user, staff id, "driver_app:<id>")

Dedup: the same vehicle + source + Manila day + message while a flag is still OPEN returns the existing flag
(created = False). A resolved flag can be raised again with the same text. Third-party trucks are not maintained by RGF
and are refused.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.fleet_health import VehicleFlag
from models.vehicle import Vehicle
from services.fleet_health import config
from services.fleet_health.matching import normalize_plate

logger = logging.getLogger("vehicle_flags")

SOURCES = ("voice", "email", "whatsapp", "checklist", "battery", "fuel", "overload", "manual", "driver_app")
SEVERITIES = ("info", "warning", "critical")
MAX_MESSAGE = 500
MAX_REF = 300


class IssueError(ValueError):
    """Invalid issue report. `status` is the HTTP status the API answers with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _open_session() -> Session:
    """The one place this module opens its own database session (the test suite replaces it so tests never touch Neon)."""
    from database import SessionLocal

    return SessionLocal()


def _clip(value, limit: int, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > limit:
        raise IssueError(f"{field} must be at most {limit} characters")
    return text


def make_dedup_key(vehicle_id: int, source: str, message: str, when: datetime | None = None) -> str:
    day = (when or datetime.now(timezone.utc)).astimezone(config.MANILA).date().isoformat()
    normalized = re.sub(r"\s+", " ", message.strip().lower())
    return hashlib.sha1(f"{vehicle_id}|{source}|{day}|{normalized}".encode("utf-8")).hexdigest()


def resolve_vehicle(db: Session, vehicle) -> Vehicle:
    if isinstance(vehicle, bool) or vehicle is None or str(vehicle).strip() == "":
        raise IssueError("vehicle is required")
    row = None
    if isinstance(vehicle, int) or (isinstance(vehicle, str) and re.fullmatch(r"-?\d+", vehicle.strip())):
        row = db.get(Vehicle, int(vehicle))
    if row is None:
        wanted = normalize_plate(vehicle)
        for candidate in db.execute(select(Vehicle)).scalars():
            if normalize_plate(candidate.plate_no) == wanted:
                row = candidate
                break
    if row is None:
        raise IssueError(f"Vehicle {vehicle!r} was not found", 404)
    if row.is_third_party:
        raise IssueError("Third-party trucks are not maintained by RGF", 422)
    return row


def serialize(flag: VehicleFlag) -> dict:
    return {
        "id": flag.id, "vehicle_id": flag.vehicle_id, "source": flag.source, "severity": flag.severity, "message": flag.message,
        "ref": flag.ref, "photo_ref": flag.photo_ref, "reported_by": flag.reported_by,
        "occurred_at": flag.occurred_at.isoformat() if flag.occurred_at else None,
        "created_at": flag.created_at.isoformat() if flag.created_at else None,
        "resolved_at": flag.resolved_at.isoformat() if flag.resolved_at else None,
        "resolved_by": flag.resolved_by, "resolution_note": flag.resolution_note,
    }


def report_issue(vehicle, source: str, severity: str, message: str, ref=None, reported_by=None, occurred_at: datetime | None = None, photo_ref=None, *, db: Session | None = None) -> dict:
    """Validate, dedup and store one issue. Returns {"flag": {...}, "created": bool}. Raises IssueError."""
    source = str(source or "").strip().lower()
    severity = str(severity or "").strip().lower()
    if source not in SOURCES:
        raise IssueError(f"source must be one of: {', '.join(SOURCES)}")
    if severity not in SEVERITIES:
        raise IssueError(f"severity must be one of: {', '.join(SEVERITIES)}")
    text = _clip(message, MAX_MESSAGE, "message")
    if not text:
        raise IssueError("message is required")
    ref = _clip(ref, MAX_REF, "ref")
    reported_by = _clip(reported_by, MAX_REF, "reported_by")
    photo_ref = _clip(photo_ref, 500, "photo_ref")
    if photo_ref and photo_ref.lower().startswith("data:"):
        raise IssueError("photo_ref must be a link or reference, not file content")
    if occurred_at is not None:
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=config.MANILA)
        if (occurred_at - datetime.now(timezone.utc)).total_seconds() > 300:
            raise IssueError("occurred_at cannot be in the future")

    owns_session = db is None
    if owns_session:
        db = _open_session()
    try:
        truck = resolve_vehicle(db, vehicle)
        key = make_dedup_key(truck.id, source, text)
        existing = db.execute(select(VehicleFlag).where(VehicleFlag.dedup_key == key, VehicleFlag.resolved_at.is_(None))).scalar_one_or_none()
        if existing is not None:
            return {"flag": serialize(existing), "created": False}
        flag = VehicleFlag(vehicle_id=truck.id, source=source, severity=severity, message=text, ref=ref, photo_ref=photo_ref, reported_by=reported_by, occurred_at=occurred_at, dedup_key=key)
        db.add(flag)
        try:
            db.flush()
        except IntegrityError:  # a concurrent report won the race for the same open dedup key
            db.rollback()
            existing = db.execute(select(VehicleFlag).where(VehicleFlag.dedup_key == key, VehicleFlag.resolved_at.is_(None))).scalar_one()
            return {"flag": serialize(existing), "created": False}
        result = {"flag": serialize(flag), "created": True}
        if owns_session:
            db.commit()
        logger.info("[FLAGS] %s %s on vehicle=%s created", severity, source, truck.plate_no)
        _invalidate_snapshot()
        return result
    finally:
        if owns_session:
            db.close()


def resolve_flag(db: Session, flag_id: int, user_id: int | None, note: str | None) -> dict:
    flag = db.get(VehicleFlag, flag_id)
    if flag is None:
        raise IssueError("Flag not found", 404)
    if flag.resolved_at is None:
        flag.resolved_at = datetime.now(timezone.utc)
        flag.resolved_by = user_id
        flag.resolution_note = _clip(note, 1000, "resolution_note")
        db.flush()
        _invalidate_snapshot()
    return serialize(flag)


def _invalidate_snapshot() -> None:
    try:
        from services.fleet_health import snapshot

        snapshot.invalidate()
    except Exception:  # noqa: BLE001 - a cache miss must never fail an issue report
        pass
