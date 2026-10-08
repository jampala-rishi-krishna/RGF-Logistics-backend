"""Fleet Health read model: loads a handful of small tables once, computes every truck's services, flags and risk, and
caches the result for 5 minutes (any edit invalidates it).

Neon queries per rebuild: 7 (vehicles, service_intervals, maintenance_records, vehicle_flags, pretrip_checklists,
vehicle_daily_stats, fuel_logs). Cartrack calls: 0 (the odometer comes from the in-memory sampler or the last daily row).
A single truck's detail view runs the same 7 queries filtered to that truck.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from models.fleet_health import FuelLog, MaintenanceRecord, PretripChecklist, ServiceInterval, VehicleDailyStat, VehicleFlag
from models.vehicle import Vehicle
from services.fleet_health import config, eco, eco_views, risk, sampler
from services.fleet_health.matching import normalize_plate

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}


# --------------------------------------------------------------------------------------------------------- helpers
def _aware(value):
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _num(value):
    return None if value is None else float(value)


def _iso(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def manila_today() -> date:
    return datetime.now(config.MANILA).date()


def _locked_reason(plate: str) -> str | None:
    try:
        from routers.fleet import LOCKED_VEHICLES

        return LOCKED_VEHICLES.get(str(plate or "").upper())
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------------------------------------- context
class Context:
    def __init__(self, today: date) -> None:
        self.today = today
        self.vehicles: list[dict] = []
        self.intervals: list[dict] = []
        self.records: list[dict] = []
        self.flags: list[dict] = []
        self.checklists: list[dict] = []
        self.daily: list[dict] = []
        self.fuel: list[dict] = []


def load_context(db: Session, vehicle_id: int | None = None, today: date | None = None, only: set[str] | None = None) -> Context:
    """`only` limits which tables are read (vehicles are always read): any of intervals, records, flags, checklists, daily, fuel."""
    today = today or manila_today()
    ctx = Context(today)
    want = lambda name: only is None or name in only  # noqa: E731
    wide_start = today - timedelta(days=400)
    for v in db.execute(select(Vehicle).order_by(Vehicle.is_third_party, Vehicle.id)).scalars():
        if vehicle_id is None or v.id == vehicle_id:
            ctx.vehicles.append({"id": v.id, "plate": v.plate_no, "plate_key": normalize_plate(v.plate_no), "is_third_party": bool(v.is_third_party), "is_gps_tracked": bool(v.is_gps_tracked),
                                 "is_reefer": v.is_reefer, "vehicle_type": v.vehicle_type, "rated_capacity_kg": _num(v.rated_capacity_kg)})
    scope = (lambda col: col == vehicle_id) if vehicle_id is not None else (lambda col: col.isnot(None))
    if want("intervals"):
        for r in db.execute(select(ServiceInterval).where(or_(ServiceInterval.vehicle_id.is_(None), scope(ServiceInterval.vehicle_id)))).scalars():
            ctx.intervals.append({"id": r.id, "vehicle_id": r.vehicle_id, "service_type": r.service_type, "interval_km": r.interval_km, "interval_engine_hours": r.interval_engine_hours,
                                  "interval_days": r.interval_days, "active": r.active, "confirmed": r.confirmed, "updated_at": _iso(r.updated_at)})
    if want("records"):
        for r in db.execute(select(MaintenanceRecord).where(scope(MaintenanceRecord.vehicle_id)).order_by(MaintenanceRecord.performed_on.desc(), MaintenanceRecord.id.desc())).scalars():
            ctx.records.append({"id": r.id, "vehicle_id": r.vehicle_id, "kind": r.kind, "service_type": r.service_type, "performed_on": r.performed_on, "odometer_km": _num(r.odometer_km),
                                "engine_hours": _num(r.engine_hours), "downtime_start": _aware(r.downtime_start), "downtime_end": _aware(r.downtime_end), "reason": r.reason, "cost_php": _num(r.cost_php),
                                "vendor": r.vendor, "notes": r.notes, "receipt_ref": r.receipt_ref, "created_by": r.created_by})
    if want("flags"):
        since30 = datetime.now(timezone.utc) - timedelta(days=30)
        flag_filter = or_(VehicleFlag.resolved_at.is_(None), VehicleFlag.created_at >= since30)
        for r in db.execute(select(VehicleFlag).where(scope(VehicleFlag.vehicle_id), flag_filter).order_by(VehicleFlag.created_at.desc())).scalars():
            ctx.flags.append({"id": r.id, "vehicle_id": r.vehicle_id, "source": r.source, "severity": r.severity, "message": r.message, "ref": r.ref, "photo_ref": r.photo_ref, "reported_by": r.reported_by,
                              "occurred_at": _aware(r.occurred_at), "created_at": _aware(r.created_at), "resolved_at": _aware(r.resolved_at), "resolved_by": r.resolved_by, "resolution_note": r.resolution_note})
    if want("checklists"):
        since_checks = datetime.now(timezone.utc) - timedelta(days=45)
        for r in db.execute(select(PretripChecklist).where(scope(PretripChecklist.vehicle_id), PretripChecklist.checked_at >= since_checks).order_by(PretripChecklist.checked_at.desc())).scalars():
            ctx.checklists.append({"id": r.id, "vehicle_id": r.vehicle_id, "staff_id": r.staff_id, "checked_at": _aware(r.checked_at), "items": r.items, "reefer_temp_c": _num(r.reefer_temp_c), "notes": r.notes, "passed": r.passed, "entered_by": r.entered_by})
    if want("daily"):
        for r in db.execute(select(VehicleDailyStat).where(scope(VehicleDailyStat.vehicle_id), VehicleDailyStat.stat_date >= wide_start).order_by(VehicleDailyStat.stat_date)).scalars():
            ctx.daily.append({"vehicle_id": r.vehicle_id, "stat_date": r.stat_date, "odometer_start_km": _num(r.odometer_start_km), "odometer_end_km": _num(r.odometer_end_km), "km_driven": _num(r.km_driven),
                              "trip_count": r.trip_count, "engine_seconds": r.engine_seconds, "idle_seconds_total": r.idle_seconds_total, "idle_seconds_at_stop": r.idle_seconds_at_stop,
                              "idle_seconds_elsewhere": r.idle_seconds_elsewhere, "speeding_events": r.speeding_events, "speeding_seconds": r.speeding_seconds, "max_speed_kmh": r.max_speed_kmh,
                              "harsh_braking": r.harsh_braking, "harsh_acceleration": r.harsh_acceleration, "harsh_cornering": r.harsh_cornering, "vext_parked_min": _num(r.vext_parked_min),
                              "vext_running_avg": _num(r.vext_running_avg), "electrical_system": r.electrical_system, "fuel_pct_start": _num(r.fuel_pct_start), "fuel_pct_end": _num(r.fuel_pct_end),
                              "refuel_events": r.refuel_events, "refuel_litres_est": _num(r.refuel_litres_est), "parked_drop_litres_est": _num(r.parked_drop_litres_est),
                              "primary_staff_id": r.primary_staff_id, "assigned": bool(r.assigned), "data_quality": r.data_quality or {}})
    if want("fuel"):
        for r in db.execute(select(FuelLog).where(scope(FuelLog.vehicle_id), FuelLog.filled_at >= datetime.now(timezone.utc) - timedelta(days=400)).order_by(FuelLog.filled_at)).scalars():
            ctx.fuel.append({"id": r.id, "vehicle_id": r.vehicle_id, "staff_id": r.staff_id, "filled_at": _aware(r.filled_at), "litres": float(r.litres), "amount_php": float(r.amount_php),
                             "odometer_km": _num(r.odometer_km), "full_tank": r.full_tank, "station": r.station, "receipt_ref": r.receipt_ref})
    return ctx


# --------------------------------------------------------------------------------------------------------- one truck
def _group(rows: list[dict], key: str = "vehicle_id") -> dict:
    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row[key], []).append(row)
    return grouped


def tracker_view(vehicle: dict, daily: list[dict]) -> dict:
    if vehicle["is_third_party"]:
        return {"kind": "third_party", "status": None, "last_seen": None, "label": "Third-party"}
    if not vehicle["is_gps_tracked"]:
        return {"kind": "no_tracker", "status": None, "last_seen": None, "label": "No tracker"}
    samples = sampler.samples_for(vehicle["plate_key"])
    if samples:
        latest = samples[-1]["ts"]
        age = (datetime.now(timezone.utc) - latest).total_seconds()
        return {"kind": "tracked", "status": "live" if age <= 1800 else "stale", "last_seen": latest.isoformat(), "label": "Live" if age <= 1800 else "No recent signal"}
    if daily:
        return {"kind": "tracked", "status": "stale", "last_seen": daily[-1]["stat_date"].isoformat(), "label": "No recent signal"}
    return {"kind": "tracked", "status": "no_data", "last_seen": None, "label": "Waiting for data"}


def current_odometer_km(vehicle: dict, daily: list[dict]) -> float | None:
    samples = sampler.samples_for(vehicle["plate_key"])
    for sample in reversed(samples):
        if sample.get("odometer_m") is not None:
            return round(sample["odometer_m"] / 1000, 1)
    for row in reversed(daily):
        if row.get("odometer_end_km") is not None:
            return row["odometer_end_km"]
    return None


def build_truck(ctx: Context, vehicle: dict, *, grouped: dict | None = None) -> dict:
    g = grouped or {"daily": _group(ctx.daily), "records": _group(ctx.records), "flags": _group(ctx.flags), "checklists": _group(ctx.checklists), "intervals": _group([i for i in ctx.intervals if i["vehicle_id"]])}
    vid = vehicle["id"]
    tracker = tracker_view(vehicle, g["daily"].get(vid, []))
    base = {"id": vid, "plate": vehicle["plate"], "vehicle_type": vehicle["vehicle_type"], "is_reefer": vehicle["is_reefer"], "tracker": tracker, "locked_reason": _locked_reason(vehicle["plate"]),
            "capacity_unconfirmed": vehicle["plate_key"] in config.UNCONFIRMED_FUEL_CAPACITY_PLATES}
    if vehicle["is_third_party"]:
        return {**base, "kind": "third_party", "message": "Third-party truck: not maintained by RGF, so it has no service tracking, risk score or eco score."}
    daily = g["daily"].get(vid, [])
    records = g["records"].get(vid, [])
    flags = g["flags"].get(vid, [])
    checklists = g["checklists"].get(vid, [])
    defaults = {i["service_type"]: i for i in ctx.intervals if i["vehicle_id"] is None}
    overrides = {i["service_type"]: i for i in g["intervals"].get(vid, [])}
    odometer = current_odometer_km(vehicle, daily) if tracker["kind"] == "tracked" else None
    services = {}
    for service_type in config.SERVICE_TYPES:
        if service_type == "reefer_service" and vehicle["is_reefer"] is not True:
            continue
        interval = risk.effective_interval(service_type, overrides, defaults)
        last = next((r for r in records if r["kind"] == "service" and r["service_type"] == service_type), None)
        hours_since = None
        if last is not None and tracker["kind"] == "tracked":
            hours_since = sum((d["engine_seconds"] or 0) for d in daily if d["stat_date"] > last["performed_on"]) / 3600.0
        status = risk.service_status(interval=interval, last_record=last, odometer_km=odometer, engine_hours_since=hours_since, today=ctx.today)
        if status["status"] != "not_tracked":
            status["label"] = config.SERVICE_LABELS[service_type]
            status["engine_hours_partial"] = bool(last and daily and last["performed_on"] < daily[0]["stat_date"])
        services[service_type] = status
    open_flags = [f for f in flags if f["resolved_at"] is None]
    cutoff30 = datetime.now(timezone.utc) - timedelta(days=config.RISK_WEIGHTS["overload_days"])
    overloads = sum(1 for f in flags if f["source"] == "overload" and f["created_at"] >= cutoff30)
    cutoff7 = datetime.now(timezone.utc) - timedelta(days=config.RISK_WEIGHTS["failed_checklist_days"])
    failed7 = sum(1 for c in checklists if not c["passed"] and c["checked_at"] >= cutoff7)
    repairs = sum(1 for r in records if r["kind"] == "repair" and (ctx.today - r["performed_on"]).days <= config.RISK_WEIGHTS["repair_days"])
    scored = risk.risk_score(service_statuses=services, open_flags=open_flags, failed_checklists_7d=failed7, overloads_30d=overloads, repairs_90d=repairs)
    last_check = checklists[0] if checklists else None
    battery_latest = None
    for r in reversed(daily):
        day_status = risk.battery_day_status(r)
        if day_status["status"] != "no_data":
            battery_latest = {**day_status, "date": r["stat_date"].isoformat(), "parked_min": r["vext_parked_min"], "running_avg": r["vext_running_avg"], "system": r["electrical_system"]}
            break
    open_repair = next((r for r in records if r["kind"] == "repair" and r["downtime_start"] and not r["downtime_end"]), None)
    return {
        **base, "kind": "tracked" if tracker["kind"] == "tracked" else "no_tracker",
        "odometer_km": odometer, "services": services, "risk": scored,
        "open_flags": [_flag_view(f) for f in open_flags], "open_flags_count": len(open_flags),
        "critical_flags_count": sum(1 for f in open_flags if f["severity"] == "critical"),
        "last_checklist": None if last_check is None else {"id": last_check["id"], "checked_at": last_check["checked_at"].isoformat(), "passed": last_check["passed"]},
        "failed_checklists_7d": failed7, "battery": battery_latest,
        "in_repair_record": open_repair is not None,
        "offer_repair_record": bool(base["locked_reason"] and "repair" in base["locked_reason"].lower() and open_repair is None),
    }


def _flag_view(f: dict) -> dict:
    return {**{k: f[k] for k in ("id", "vehicle_id", "source", "severity", "message", "ref", "photo_ref", "reported_by", "resolution_note", "resolved_by")},
            "occurred_at": _iso(f["occurred_at"]), "created_at": _iso(f["created_at"]), "resolved_at": _iso(f["resolved_at"])}


# --------------------------------------------------------------------------------------------------------- fleet
def last_full_week(today: date) -> date:
    return eco.week_start(today) - timedelta(days=7)


def build_snapshot(db: Session) -> dict:
    ctx = load_context(db)
    grouped = {"daily": _group(ctx.daily), "records": _group(ctx.records), "flags": _group(ctx.flags), "checklists": _group(ctx.checklists), "intervals": _group([i for i in ctx.intervals if i["vehicle_id"]])}
    trucks = [build_truck(ctx, v, grouped=grouped) for v in ctx.vehicles]
    maintained = [t for t in trucks if t["kind"] in ("tracked", "no_tracker")]
    services_overdue = sum(1 for t in maintained for s in t["services"].values() if s["status"] == "overdue")
    needing = [t for t in maintained if t["risk"]["needs_attention"]]
    open_issues = sum(t["open_flags_count"] for t in maintained)
    plates = {v["id"]: v["plate"] for v in ctx.vehicles}
    # km/L over the last 30 days and CO2 this month: from the fuel log only
    intervals_by_vehicle = eco_views.kmpl_intervals(ctx.fuel)
    all_intervals = [i for items in intervals_by_vehicle.values() for i in items]
    kmpl_30 = eco_views.aggregate_kmpl(eco_views.intervals_ending_between(all_intervals, ctx.today - timedelta(days=29), ctx.today))
    month_start = ctx.today.replace(day=1)
    month_fills = [f for f in ctx.fuel if f["filled_at"].astimezone(config.MANILA).date() >= month_start and f["vehicle_id"] in plates]
    month_litres = round(sum(f["litres"] for f in month_fills), 2) if month_fills else None
    week = last_full_week(ctx.today)
    scores = eco_views.driver_scores(ctx.daily, ctx.fuel, week, plate_of=plates)
    scored = [s["score"] for s in scores if s["score"] is not None]
    tracked_defaults = [i for i in ctx.intervals if i["vehicle_id"] is None and i["active"]]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trucks": trucks,
        "needs_attention": [t["id"] for t in sorted(needing, key=lambda t: -t["risk"]["score"])],
        "kpis": {
            "trucks_needing_attention": len(needing), "maintained_trucks": len(maintained), "services_overdue": services_overdue, "open_issues": open_issues,
            "fleet_kmpl_30d": kmpl_30, "co2_month_kg": eco.co2_kg(month_litres), "co2_month_litres": month_litres,
            "avg_eco_score_last_week": round(sum(scored) / len(scored)) if scored else None, "eco_scored_drivers": len(scored), "eco_week_start": week.isoformat(),
        },
        "intervals_unconfirmed": any(not i["confirmed"] for i in tracked_defaults),
        "service_types": [{"key": k, "label": config.SERVICE_LABELS[k]} for k in config.SERVICE_TYPES],
        "data_through": (max(d["stat_date"] for d in ctx.daily).isoformat() if ctx.daily else None),
    }


def get_snapshot(db: Session, *, force: bool = False) -> dict:
    with _lock:
        fresh = _cache["data"] is not None and time.monotonic() - _cache["at"] < config.SNAPSHOT_TTL_SECONDS
        if fresh and not force:
            return _cache["data"]
    data = build_snapshot(db)
    with _lock:
        _cache.update(at=time.monotonic(), data=data)
    return data


def invalidate() -> None:
    with _lock:
        _cache.update(at=0.0, data=None)


# --------------------------------------------------------------------------------------------------------- detail
def build_detail(db: Session, vehicle_id: int) -> dict | None:
    ctx = load_context(db, vehicle_id=vehicle_id)
    if not ctx.vehicles:
        return None
    vehicle = ctx.vehicles[0]
    truck = build_truck(ctx, vehicle)
    if truck["kind"] == "third_party":
        return {"truck": truck}
    daily = [d for d in ctx.daily if d["stat_date"] >= ctx.today - timedelta(days=90)]
    defaults = {i["service_type"]: i for i in ctx.intervals if i["vehicle_id"] is None}
    overrides = {i["service_type"]: i for i in ctx.intervals if i["vehicle_id"] == vehicle_id}
    intervals_view = []
    for service_type in config.SERVICE_TYPES:
        if service_type == "reefer_service" and vehicle["is_reefer"] is not True:
            continue
        default, override = defaults.get(service_type), overrides.get(service_type)
        intervals_view.append({"service_type": service_type, "label": config.SERVICE_LABELS[service_type], "default": default, "override": override, "effective": override or default,
                               "confirmed": bool((override or default or {}).get("confirmed"))})
    battery_series = []
    for row in daily:
        status = risk.battery_day_status(row)
        battery_series.append({"date": row["stat_date"].isoformat(), "parked_min": row["vext_parked_min"], "running_avg": row["vext_running_avg"], "system": row["electrical_system"], "status": status["status"], "reasons": status["reasons"]})
    fuel = [f for f in ctx.fuel]
    fuel_view = []
    for index, fill in enumerate(sorted(fuel, key=lambda f: f["filled_at"], reverse=True)):
        fuel_view.append({**fill, "filled_at": fill["filled_at"].isoformat(), "price_per_litre": round(fill["amount_php"] / fill["litres"], 2) if fill["litres"] else None})
    intervals = eco.full_to_full(fuel)
    capacity = next((d["data_quality"].get("fuel_capacity_l") for d in reversed(ctx.daily) if d["data_quality"].get("fuel_capacity_l")), None)
    return {
        "truck": truck,
        "records": [{**r, "performed_on": r["performed_on"].isoformat(), "downtime_start": _iso(r["downtime_start"]), "downtime_end": _iso(r["downtime_end"])} for r in ctx.records],
        "intervals": intervals_view,
        "flags": [_flag_view(f) for f in ctx.flags],
        "checklists": [{**c, "checked_at": c["checked_at"].isoformat()} for c in ctx.checklists],
        "daily": [{"date": d["stat_date"].isoformat(), "km": d["km_driven"], "engine_hours": round((d["engine_seconds"] or 0) / 3600, 2), "trips": d["trip_count"], "assigned": d["assigned"],
                   "idle_total_min": round((d["idle_seconds_total"] or 0) / 60, 1), "speeding_seconds": d["speeding_seconds"], "harsh": (d["harsh_braking"] or 0) + (d["harsh_acceleration"] or 0) + (d["harsh_cornering"] or 0),
                   "partial_day": bool((d["data_quality"] or {}).get("partial_day"))} for d in daily],
        "battery_series": battery_series,
        "fuel_logs": fuel_view,
        "kmpl_intervals": [{**i, "start_at": i["start_at"].isoformat(), "end_at": i["end_at"].isoformat()} for i in intervals],
        "fuel_checks": eco.fuel_log_checks(fuel, capacity, truck["capacity_unconfirmed"]),
        "has_tracker": truck["kind"] == "tracked",
    }
