"""Fleet Health API: maintenance + eco driving.

Roles (planner is an alias of dispatcher in the auth layer, so it behaves like dispatcher here):
  admin, dispatcher  view + edit everything
  warehouse          view + create pre-trip checklists, fuel logs and issue reports
Reads come from a 5-minute cached read model (services/fleet_health/snapshot.py); every edit invalidates it.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, require_role
from database import get_db
from services import vehicle_flags
from services.cartrack_limiter import CartrackCallCounter
from services.fleet_health import config, eco, eco_views, ops, sampler, scorecard, service, snapshot
from services.fleet_health.matching import match_fleet
from services.vehicle_flags import IssueError

router = APIRouter(prefix="/api/fleet-health", tags=["fleet-health"])

VIEW = require_role("admin", "dispatcher", "warehouse")
EDIT = require_role("admin", "dispatcher")
ENTRY = require_role("admin", "dispatcher", "warehouse")
ADMIN = require_role("admin")


def _fail(db: Session, exc: IssueError) -> HTTPException:
    db.rollback()
    return HTTPException(exc.status, str(exc))


def _commit(db: Session) -> None:
    db.commit()
    snapshot.invalidate()


def _monday(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return eco.week_start(date.fromisoformat(value))
    except ValueError:
        raise HTTPException(400, "week must be YYYY-MM-DD") from None


def _date(value: str | None, default: date, field: str) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"{field} must be YYYY-MM-DD") from None


# ------------------------------------------------------------------------------------------------ read: maintenance
@router.get("/summary", dependencies=[Depends(VIEW)])
def summary(db: Session = Depends(get_db)):
    data = snapshot.get_snapshot(db)
    attention = [t for t in data["trucks"] if t["id"] in set(data["needs_attention"])]
    return {
        "generated_at": data["generated_at"], "data_through": data["data_through"], "kpis": data["kpis"], "intervals_unconfirmed": data["intervals_unconfirmed"],
        "needs_attention": [{"id": t["id"], "plate": t["plate"], "risk": t["risk"], "overdue": [s["label"] for s in t["services"].values() if s["status"] == "overdue"], "critical_flags": t["critical_flags_count"]} for t in attention],
        "risk_weights": config.RISK_WEIGHTS, "eco_weights": config.ECO_WEIGHTS, "eco_min_km": config.ECO_MIN_KM_PER_WEEK, "co2_kg_per_litre": config.CO2_KG_PER_LITRE_DIESEL,
    }


@router.get("/trucks", dependencies=[Depends(VIEW)])
def trucks(db: Session = Depends(get_db)):
    data = snapshot.get_snapshot(db)
    return {"generated_at": data["generated_at"], "service_types": data["service_types"], "intervals_unconfirmed": data["intervals_unconfirmed"], "trucks": data["trucks"]}


@router.get("/trucks/{vehicle_id}", dependencies=[Depends(VIEW)])
def truck_detail(vehicle_id: int, db: Session = Depends(get_db)):
    detail = snapshot.build_detail(db, vehicle_id)
    if detail is None:
        raise HTTPException(404, "Vehicle not found")
    return detail


# ------------------------------------------------------------------------------------------------ read: eco
@router.get("/eco/drivers", dependencies=[Depends(VIEW)])
def eco_drivers(week: str | None = Query(None, description="Any date inside the wanted Mon-Sun week; default = last full week"), db: Session = Depends(get_db)):
    ctx = snapshot.load_context(db, only={"daily", "fuel"})
    start = _monday(week) or snapshot.last_full_week(ctx.today)
    plates = {v["id"]: v["plate"] for v in ctx.vehicles}
    drivers = eco_views.driver_scores(ctx.daily, ctx.fuel, start, plate_of=plates)
    intervals = [i for items in eco_views.kmpl_intervals(ctx.fuel).values() for i in items]
    end = start + timedelta(days=6)
    scored = [d["score"] for d in drivers if d["score"] is not None]
    unassigned = eco.rollup([r for r in ctx.daily if start <= r["stat_date"] <= end and not r.get("assigned")])
    return {"week_start": start.isoformat(), "week_end": end.isoformat(), "min_km": config.ECO_MIN_KM_PER_WEEK, "weights": config.ECO_WEIGHTS,
            "fleet_kmpl_30d": eco_views.aggregate_kmpl(eco_views.intervals_ending_between(intervals, end - timedelta(days=29), end)), "drivers": drivers,
            "summary": {"avg_score": round(sum(scored) / len(scored)) if scored else None, "scored": len(scored), "not_enough_data": len(drivers) - len(scored)},
            "unassigned": {"km": unassigned["km"], "days": unassigned["days"], "note": "Days with no assignment data, ambiguous multi-driver assignment, or unknown distance are excluded from individual driver scores; idle on unassigned days is not classified."}}


@router.get("/eco/trucks", dependencies=[Depends(VIEW)])
def eco_trucks(from_: str | None = Query(None, alias="from"), to: str | None = None, db: Session = Depends(get_db)):
    ctx = snapshot.load_context(db, only={"daily", "fuel"})
    last = _date(to, ctx.today - timedelta(days=1), "to")
    first = _date(from_, last - timedelta(days=29), "from")
    if first > last:
        raise HTTPException(400, "from must be on or before to")
    tracked = [{"id": v["id"], "plate": v["plate"], "capacity_unconfirmed": v["plate_key"] in config.UNCONFIRMED_FUEL_CAPACITY_PLATES} for v in ctx.vehicles if v["is_gps_tracked"] and not v["is_third_party"]]
    rollups = eco_views.truck_rollups(ctx.daily, ctx.fuel, first, last, vehicles=tracked)
    fleet = eco.rollup([r for r in ctx.daily if first <= r["stat_date"] <= last and r["vehicle_id"] in {t["id"] for t in tracked}])
    return {"from": first.isoformat(), "to": last.isoformat(), "trucks": rollups, "fleet": fleet}


@router.get("/eco/fuel-checks", dependencies=[Depends(VIEW)])
def eco_fuel_checks(days: int = Query(30, ge=1, le=120), db: Session = Depends(get_db)):
    """Sensor-based refuel estimates and parked fuel drops (analog gauge: estimates, worded as checks) plus odd fuel-log fills."""
    ctx = snapshot.load_context(db, only={"daily", "fuel"})
    since = ctx.today - timedelta(days=days)
    plates = {v["id"]: v["plate"] for v in ctx.vehicles}
    unconfirmed = {v["id"] for v in ctx.vehicles if v["plate_key"] in config.UNCONFIRMED_FUEL_CAPACITY_PLATES}
    sensor = eco_views.sensor_fuel_checks([r for r in ctx.daily if r["stat_date"] >= since], plates=plates, unconfirmed=unconfirmed)
    log_checks = []
    for vehicle_id, fills in eco_views.fills_by_vehicle(ctx.fuel).items():
        capacity = next((d["data_quality"].get("fuel_capacity_l") for d in reversed(ctx.daily) if d["vehicle_id"] == vehicle_id and d["data_quality"].get("fuel_capacity_l")), None)
        for check in eco.fuel_log_checks(fills, capacity, vehicle_id in unconfirmed):
            log_checks.append({**check, "vehicle_id": vehicle_id, "plate": plates.get(vehicle_id), "filled_at": check["filled_at"].isoformat()})
    return {"days": days, "sensor_checks": sensor, "log_checks": sorted(log_checks, key=lambda c: c["filled_at"], reverse=True), "label": "Estimates from the analog fuel sensor: they are checks, not conclusions."}


@router.get("/eco/co2", dependencies=[Depends(VIEW)])
def eco_co2(months: int = Query(12, ge=1, le=24), db: Session = Depends(get_db)):
    ctx = snapshot.load_context(db, only={"fuel"})
    plates = {v["id"]: v["plate"] for v in ctx.vehicles}
    buckets: dict[str, dict] = {}
    for fill in ctx.fuel:
        key = fill["filled_at"].astimezone(config.MANILA).strftime("%Y-%m")
        entry = buckets.setdefault(key, {"month": key, "litres": 0.0, "trucks": {}})
        entry["litres"] += fill["litres"]
        entry["trucks"][plates.get(fill["vehicle_id"], str(fill["vehicle_id"]))] = entry["trucks"].get(plates.get(fill["vehicle_id"], str(fill["vehicle_id"])), 0.0) + fill["litres"]
    series = []
    for key in sorted(buckets)[-months:]:
        entry = buckets[key]
        series.append({"month": key, "litres": round(entry["litres"], 2), "co2_kg": eco.co2_kg(entry["litres"]), "trucks": {p: {"litres": round(l, 2), "co2_kg": eco.co2_kg(l)} for p, l in sorted(entry["trucks"].items())}})
    return {"kg_per_litre": config.CO2_KG_PER_LITRE_DIESEL, "formula": "CO2 (kg) = diesel litres from the fuel log x 2.68", "months": series}


@router.get("/fuel-logs", dependencies=[Depends(VIEW)])
def list_fuel_logs(vehicle_id: int | None = None, limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    """Fuel fills, newest first, with price per litre (computed on read) and the full-to-full km/L each fill closes."""
    ctx = snapshot.load_context(db, vehicle_id=vehicle_id, only={"daily", "fuel"})
    plates = {v["id"]: v["plate"] for v in ctx.vehicles}
    unconfirmed = {v["id"] for v in ctx.vehicles if v["plate_key"] in config.UNCONFIRMED_FUEL_CAPACITY_PLATES}
    closes: dict[int, dict] = {}
    for vid, fills in eco_views.fills_by_vehicle(ctx.fuel).items():
        for interval in eco.full_to_full(fills):
            closing = next((f for f in fills if f["filled_at"] == interval["end_at"]), None)
            if closing:
                closes[closing["id"]] = interval
    rows = []
    for fill in sorted(ctx.fuel, key=lambda f: f["filled_at"], reverse=True)[:limit]:
        interval = closes.get(fill["id"])
        capacity = next((d["data_quality"].get("fuel_capacity_l") for d in reversed(ctx.daily) if d["vehicle_id"] == fill["vehicle_id"] and d["data_quality"].get("fuel_capacity_l")), None)
        checks = eco.fuel_log_checks([fill], capacity, fill["vehicle_id"] in unconfirmed)
        low, high = config.KMPL_PLAUSIBLE_RANGE
        if interval and not (low <= interval["kmpl"] <= high):
            checks.append({"reason": f"{interval['kmpl']} km/L over {interval['km']:.0f} km looks unusual (expected {low}-{high}), check odometer or litres"})
        rows.append({**fill, "filled_at": fill["filled_at"].isoformat(), "plate": plates.get(fill["vehicle_id"]), "price_per_litre": round(fill["amount_php"] / fill["litres"], 2) if fill["litres"] else None,
                     "kmpl": interval["kmpl"] if interval else None, "interval_km": interval["km"] if interval else None, "check": checks[0]["reason"] if checks else None,
                     "capacity_unconfirmed": fill["vehicle_id"] in unconfirmed})
    return {"count": len(rows), "logs": rows}


# ------------------------------------------------------------------------------------------------ writes
@router.post("/maintenance-records", status_code=201)
def create_record(body: dict = Body(...), user: CurrentUser = Depends(EDIT), db: Session = Depends(get_db)):
    try:
        result = ops.save_record(db, body, user.id)
    except IssueError as exc:
        raise _fail(db, exc) from exc
    _commit(db)
    return result


@router.put("/maintenance-records/{record_id}")
def update_record(record_id: int, body: dict = Body(...), user: CurrentUser = Depends(EDIT), db: Session = Depends(get_db)):
    try:
        result = ops.save_record(db, body, user.id, record_id)
    except IssueError as exc:
        raise _fail(db, exc) from exc
    _commit(db)
    return result


@router.delete("/maintenance-records/{record_id}", status_code=204)
def delete_record(record_id: int, user: CurrentUser = Depends(EDIT), db: Session = Depends(get_db)):
    try:
        ops.delete_record(db, record_id)
    except IssueError as exc:
        raise _fail(db, exc) from exc
    _commit(db)
    return Response(status_code=204)


@router.put("/service-intervals")
def put_interval(body: dict = Body(...), user: CurrentUser = Depends(EDIT), db: Session = Depends(get_db)):
    """Upsert a fleet default (vehicle_id omitted/null) or a per-truck override. Saving confirms it unless confirmed=false."""
    try:
        result = ops.save_interval(db, body, user.id)
    except IssueError as exc:
        raise _fail(db, exc) from exc
    _commit(db)
    return result


@router.delete("/service-intervals/{interval_id}", status_code=204)
def remove_interval(interval_id: int, user: CurrentUser = Depends(EDIT), db: Session = Depends(get_db)):
    try:
        ops.delete_interval(db, interval_id)
    except IssueError as exc:
        raise _fail(db, exc) from exc
    _commit(db)
    return Response(status_code=204)


@router.post("/fuel-logs", status_code=201)
def create_fuel_log(body: dict = Body(...), user: CurrentUser = Depends(ENTRY), db: Session = Depends(get_db)):
    try:
        result = ops.save_fuel_log(db, body, user.id)
    except IssueError as exc:
        raise _fail(db, exc) from exc
    _commit(db)
    return result


@router.put("/fuel-logs/{log_id}")
def update_fuel_log(log_id: int, body: dict = Body(...), user: CurrentUser = Depends(EDIT), db: Session = Depends(get_db)):
    try:
        result = ops.save_fuel_log(db, body, user.id, log_id)
    except IssueError as exc:
        raise _fail(db, exc) from exc
    _commit(db)
    return result


@router.delete("/fuel-logs/{log_id}", status_code=204)
def remove_fuel_log(log_id: int, user: CurrentUser = Depends(EDIT), db: Session = Depends(get_db)):
    try:
        ops.delete_fuel_log(db, log_id)
    except IssueError as exc:
        raise _fail(db, exc) from exc
    _commit(db)
    return Response(status_code=204)


@router.post("/pretrip-checklists", status_code=201)
def create_checklist(body: dict = Body(...), user: CurrentUser = Depends(ENTRY), db: Session = Depends(get_db)):
    try:
        result = ops.save_checklist(db, body, user.id, user.full_name or user.email)
    except IssueError as exc:
        raise _fail(db, exc) from exc
    _commit(db)
    return result


@router.post("/flags/{flag_id}/resolve")
def resolve(flag_id: int, body: dict = Body(default={}), user: CurrentUser = Depends(EDIT), db: Session = Depends(get_db)):
    try:
        result = vehicle_flags.resolve_flag(db, flag_id, user.id, (body or {}).get("note"))
    except IssueError as exc:
        raise _fail(db, exc) from exc
    _commit(db)
    return result


class IssueBody(BaseModel):
    """POST /api/fleet-health/issues - the contract for every issue source (documented in docs/FLEET_HEALTH_ISSUES_API.md)."""
    vehicle: int | str = Field(description="vehicles.id or the plate")
    source: str = Field(description="manual | checklist | battery | fuel | overload | voice | email | whatsapp | driver_app")
    severity: str = Field(description="info | warning | critical")
    message: str = Field(max_length=500)
    ref: str | None = Field(None, max_length=300)
    photo_ref: str | None = Field(None, max_length=500, description="link/reference only - no file content")
    reported_by: str | None = Field(None, max_length=300)
    occurred_at: datetime | None = None


@router.post("/issues", status_code=201)
def post_issue(body: IssueBody, response: Response, user: CurrentUser = Depends(ENTRY), db: Session = Depends(get_db)):
    try:
        result = vehicle_flags.report_issue(body.vehicle, body.source, body.severity, body.message, ref=body.ref, reported_by=body.reported_by or user.full_name or user.email, occurred_at=body.occurred_at, photo_ref=body.photo_ref, db=db)
    except IssueError as exc:
        raise _fail(db, exc) from exc
    _commit(db)
    if not result["created"]:
        response.status_code = 200   # an identical open issue already existed: nothing new was stored
    return result


# ------------------------------------------------------------------------------------------------ admin
@router.get("/matching", dependencies=[Depends(VIEW)])
async def matching(db: Session = Depends(get_db)):
    """Which RareChain vehicles have a Cartrack tracker (1 Cartrack call: /rest/vehicles)."""
    counter = CartrackCallCounter()
    fleet = await match_fleet(db, counter=counter)
    return {**fleet, "cartrack_calls": counter.calls, "unconfirmed_fuel_capacity": sorted(config.UNCONFIRMED_FUEL_CAPACITY_PLATES)}


@router.get("/sampler", dependencies=[Depends(VIEW)])
def sampler_status():
    return {**sampler.last_run(), "started_at": sampler.started_at().isoformat(), "interval_seconds": config.SAMPLE_INTERVAL_SECONDS}


@router.post("/backfill", dependencies=[Depends(ADMIN)])
async def backfill(
    dry_run: bool = Query(True, description="Default true: compute and return the rows, write nothing"),
    days: int = Query(config.BACKFILL_DAYS_DEFAULT, ge=1, le=90),
    sample: int = Query(3, ge=0, le=50, description="How many computed rows to include in the response"),
    db: Session = Depends(get_db),
):
    """Admin only. Last `days` complete Manila days from Cartrack trips. dry_run=true (default) never writes."""
    result = await service.run_backfill(db, days, dry_run=dry_run)
    rows = result.pop("rows", [])
    return {**result, "sample_rows": rows[:sample]}


class ScorecardBody(BaseModel):
    enabled: bool


@router.get("/eco/scorecard", dependencies=[Depends(VIEW)])
def scorecard_status(week: str | None = Query(None), db: Session = Depends(get_db)):
    """Switch state + the message each driver WOULD get (nothing is sent by this call)."""
    return {"switch": scorecard.status(), "preview": scorecard.preview(db, _monday(week))}


@router.put("/eco/scorecard")
def scorecard_switch(body: ScorecardBody, user: CurrentUser = Depends(ADMIN)):
    return scorecard.set_enabled(body.enabled, user.full_name or user.email)
