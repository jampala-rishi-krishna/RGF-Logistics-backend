"""Validated writes for Fleet Health: maintenance records, service intervals, fuel logs, pre-trip checklists.

Every function takes the caller's session, validates, flushes, and returns a plain dict. The router commits and invalidates the
read-model cache. Input problems raise vehicle_flags.IssueError (the router maps `.status` to the HTTP status).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.fleet_health import FuelLog, MaintenanceRecord, PretripChecklist, ServiceInterval
from models.vehicle import Vehicle
from services import vehicle_flags
from services.fleet_health import config
from services.vehicle_flags import IssueError

KINDS = ("service", "repair", "inspection")


def _vehicle(db: Session, vehicle_id) -> Vehicle:
    row = db.get(Vehicle, int(vehicle_id)) if vehicle_id is not None else None
    if row is None:
        raise IssueError("Vehicle not found", 404)
    if row.is_third_party:
        raise IssueError("Third-party trucks are not maintained by RGF", 422)
    return row


def _text(value, limit: int, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > limit:
        raise IssueError(f"{field} must be at most {limit} characters")
    return text


def _ref(value, field: str) -> str | None:
    text = _text(value, 500, field)
    if text and text.lower().startswith("data:"):
        raise IssueError(f"{field} must be a link or reference, not file content")
    return text


def _number(value, field: str, *, minimum=None, maximum=None, required=False):
    if value is None or value == "":
        if required:
            raise IssueError(f"{field} is required")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise IssueError(f"{field} must be a number") from None
    if minimum is not None and number < minimum:
        raise IssueError(f"{field} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise IssueError(f"{field} must be at most {maximum}")
    return number


def _aware(value, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise IssueError(f"{field} must be an ISO date-time") from None
    if value.tzinfo is None:
        value = value.replace(tzinfo=config.MANILA)
    return value


def _not_future(moment: datetime, field: str) -> None:
    if moment > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise IssueError(f"{field} cannot be in the future")


# ---------------------------------------------------------------------------------------------------------
def save_record(db: Session, data: dict, user_id: int | None, record_id: int | None = None) -> dict:
    kind = str(data.get("kind") or "").lower()
    if kind not in KINDS:
        raise IssueError(f"kind must be one of: {', '.join(KINDS)}")
    service_type = data.get("service_type") or None
    if kind == "service":
        if service_type not in config.SERVICE_TYPES:
            raise IssueError(f"service_type must be one of: {', '.join(config.SERVICE_TYPES)}")
    else:
        service_type = None
    performed_on = data.get("performed_on")
    if isinstance(performed_on, str):
        try:
            performed_on = date.fromisoformat(performed_on[:10])
        except ValueError:
            raise IssueError("performed_on must be YYYY-MM-DD") from None
    if not isinstance(performed_on, date):
        raise IssueError("performed_on is required")
    if performed_on > datetime.now(config.MANILA).date():
        raise IssueError("performed_on cannot be in the future")
    start, end = _aware(data.get("downtime_start"), "downtime_start"), _aware(data.get("downtime_end"), "downtime_end")
    if end and not start:
        raise IssueError("downtime_end needs downtime_start")
    if start and end and end < start:
        raise IssueError("downtime_end must be after downtime_start")
    values = dict(
        kind=kind, service_type=service_type, performed_on=performed_on,
        odometer_km=_number(data.get("odometer_km"), "odometer_km", minimum=0, maximum=5_000_000),
        engine_hours=_number(data.get("engine_hours"), "engine_hours", minimum=0, maximum=500_000),
        downtime_start=start, downtime_end=end, reason=_text(data.get("reason"), 1000, "reason"),
        cost_php=_number(data.get("cost_php"), "cost_php", minimum=0, maximum=100_000_000), vendor=_text(data.get("vendor"), 200, "vendor"),
        notes=_text(data.get("notes"), 2000, "notes"), receipt_ref=_ref(data.get("receipt_ref"), "receipt_ref"),
    )
    if record_id is None:
        _vehicle(db, data.get("vehicle_id"))
        row = MaintenanceRecord(vehicle_id=int(data["vehicle_id"]), created_by=user_id, **values)
        db.add(row)
    else:
        row = db.get(MaintenanceRecord, record_id)
        if row is None:
            raise IssueError("Record not found", 404)
        for key, value in values.items():
            setattr(row, key, value)
    db.flush()
    return record_dict(row)


def record_dict(r: MaintenanceRecord) -> dict:
    return {"id": r.id, "vehicle_id": r.vehicle_id, "kind": r.kind, "service_type": r.service_type, "performed_on": r.performed_on.isoformat(),
            "odometer_km": None if r.odometer_km is None else float(r.odometer_km), "engine_hours": None if r.engine_hours is None else float(r.engine_hours),
            "downtime_start": r.downtime_start.isoformat() if r.downtime_start else None, "downtime_end": r.downtime_end.isoformat() if r.downtime_end else None,
            "reason": r.reason, "cost_php": None if r.cost_php is None else float(r.cost_php), "vendor": r.vendor, "notes": r.notes, "receipt_ref": r.receipt_ref}


def delete_record(db: Session, record_id: int) -> None:
    row = db.get(MaintenanceRecord, record_id)
    if row is None:
        raise IssueError("Record not found", 404)
    db.delete(row)


# ---------------------------------------------------------------------------------------------------------
def save_interval(db: Session, data: dict, user_id: int | None) -> dict:
    """Upsert a fleet default (vehicle_id null) or a per-truck override. Saving marks it confirmed unless confirmed=false is sent."""
    service_type = data.get("service_type")
    if service_type not in config.SERVICE_TYPES:
        raise IssueError(f"service_type must be one of: {', '.join(config.SERVICE_TYPES)}")
    vehicle_id = data.get("vehicle_id")
    if vehicle_id is not None:
        _vehicle(db, vehicle_id)
        vehicle_id = int(vehicle_id)
    km = _number(data.get("interval_km"), "interval_km", minimum=1, maximum=1_000_000)
    hours = _number(data.get("interval_engine_hours"), "interval_engine_hours", minimum=1, maximum=100_000)
    days = _number(data.get("interval_days"), "interval_days", minimum=1, maximum=3650)
    if km is None and hours is None and days is None:
        raise IssueError("Set at least one of interval_km, interval_engine_hours, interval_days")
    row = db.execute(select(ServiceInterval).where(ServiceInterval.service_type == service_type, ServiceInterval.vehicle_id.is_(None) if vehicle_id is None else ServiceInterval.vehicle_id == vehicle_id)).scalar_one_or_none()
    if row is None:
        row = ServiceInterval(vehicle_id=vehicle_id, service_type=service_type)
        db.add(row)
    row.interval_km, row.interval_engine_hours, row.interval_days = (int(km) if km else None), (int(hours) if hours else None), (int(days) if days else None)
    row.active = bool(data.get("active", True))
    row.confirmed = bool(data.get("confirmed", True))
    row.updated_by = user_id
    db.flush()
    return interval_dict(row)


def interval_dict(r: ServiceInterval) -> dict:
    return {"id": r.id, "vehicle_id": r.vehicle_id, "service_type": r.service_type, "interval_km": r.interval_km, "interval_engine_hours": r.interval_engine_hours, "interval_days": r.interval_days, "active": r.active, "confirmed": r.confirmed}


def delete_interval(db: Session, interval_id: int) -> None:
    row = db.get(ServiceInterval, interval_id)
    if row is None:
        raise IssueError("Interval not found", 404)
    if row.vehicle_id is None:
        raise IssueError("Fleet defaults cannot be deleted; edit them instead", 422)
    db.delete(row)


# ---------------------------------------------------------------------------------------------------------
def save_fuel_log(db: Session, data: dict, user_id: int | None, log_id: int | None = None) -> dict:
    filled_at = _aware(data.get("filled_at"), "filled_at")
    if filled_at is None:
        raise IssueError("filled_at is required")
    _not_future(filled_at, "filled_at")
    litres = _number(data.get("litres"), "litres", minimum=0.01, maximum=1000, required=True)
    amount = _number(data.get("amount_php"), "amount_php", minimum=0, maximum=10_000_000, required=True)
    values = dict(filled_at=filled_at, litres=litres, amount_php=amount, odometer_km=_number(data.get("odometer_km"), "odometer_km", minimum=0, maximum=5_000_000),
                  full_tank=bool(data.get("full_tank")), station=_text(data.get("station"), 200, "station"), receipt_ref=_ref(data.get("receipt_ref"), "receipt_ref"),
                  staff_id=int(data["staff_id"]) if data.get("staff_id") not in (None, "") else None)
    if log_id is None:
        _vehicle(db, data.get("vehicle_id"))
        row = FuelLog(vehicle_id=int(data["vehicle_id"]), created_by=user_id, **values)
        db.add(row)
    else:
        row = db.get(FuelLog, log_id)
        if row is None:
            raise IssueError("Fuel log not found", 404)
        for key, value in values.items():
            setattr(row, key, value)
    db.flush()
    return fuel_dict(row)


def fuel_dict(r: FuelLog) -> dict:
    litres, amount = float(r.litres), float(r.amount_php)
    return {"id": r.id, "vehicle_id": r.vehicle_id, "staff_id": r.staff_id, "filled_at": r.filled_at.isoformat(), "litres": litres, "amount_php": amount, "price_per_litre": round(amount / litres, 2) if litres else None,
            "odometer_km": None if r.odometer_km is None else float(r.odometer_km), "full_tank": r.full_tank, "station": r.station, "receipt_ref": r.receipt_ref}


def delete_fuel_log(db: Session, log_id: int) -> None:
    row = db.get(FuelLog, log_id)
    if row is None:
        raise IssueError("Fuel log not found", 404)
    db.delete(row)


# ---------------------------------------------------------------------------------------------------------
def save_checklist(db: Session, data: dict, user_id: int | None, entered_by_name: str | None) -> dict:
    vehicle = _vehicle(db, data.get("vehicle_id"))
    raw_items = data.get("items") or {}
    unknown = set(raw_items) - set(config.CHECKLIST_ITEMS)
    if unknown:
        raise IssueError(f"Unknown checklist item(s): {', '.join(sorted(unknown))}")
    items = {}
    for key in config.CHECKLIST_ITEMS:
        value = str(raw_items.get(key, "na")).lower()
        if value not in config.CHECKLIST_VALUES:
            raise IssueError(f"{key} must be one of: {', '.join(config.CHECKLIST_VALUES)}")
        items[key] = value
    issues = [k for k, v in items.items() if v == "issue"]
    checked_at = _aware(data.get("checked_at"), "checked_at") or datetime.now(timezone.utc)
    _not_future(checked_at, "checked_at")
    row = PretripChecklist(vehicle_id=vehicle.id, staff_id=int(data["staff_id"]) if data.get("staff_id") not in (None, "") else None, checked_at=checked_at, entered_by=user_id,
                           items=items, reefer_temp_c=_number(data.get("reefer_temp_c"), "reefer_temp_c", minimum=-60, maximum=60), notes=_text(data.get("notes"), 1000, "notes"), passed=not issues)
    db.add(row)
    db.flush()
    result = {"id": row.id, "vehicle_id": vehicle.id, "checked_at": checked_at.isoformat(), "items": items, "passed": row.passed, "reefer_temp_c": None if row.reefer_temp_c is None else float(row.reefer_temp_c), "notes": row.notes, "flag": None}
    if issues:
        labels = ", ".join(i.replace("_", " ") for i in issues)
        flag = vehicle_flags.report_issue(vehicle.id, "checklist", "critical" if "brakes" in issues else "warning", f"Pre-trip check failed: {labels}." + (f" {row.notes}" if row.notes else ""),
                                          ref=f"checklist:{row.id}", reported_by=entered_by_name, occurred_at=checked_at, db=db)
        result["flag"] = flag
    return result
