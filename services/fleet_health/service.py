"""Nightly job + backfill for vehicle_daily_stats.

Cartrack calls: nightly = 1 (/rest/vehicles roster) + 1 trips page per tracked vehicle (limit=100 rows/page, normally
one page per truck per day). Backfill = 1 roster call + ceil(trips/limit) pages per tracked vehicle for the WHOLE range.
Neon: reads vehicles (14 rows) and that day's assigned sales_orders; writes at most one row per tracked vehicle per day.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.fleet_health import VehicleDailyStat
from models.sales_order_history import SalesOrderHistory
from services import cartrack_client
from services.cartrack_limiter import CartrackCallCounter
from services.fleet_health import automation, config, daily, sampler, snapshot
from services.fleet_health.matching import match_fleet

logger = logging.getLogger("fleet_health")

_run_lock = asyncio.Lock()
_geocode_cache: dict[str, tuple[float, float] | None] = {}


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def day_bounds(first: date, last: date) -> tuple[datetime, datetime]:
    """[first 00:00, last+1 00:00) in Manila."""
    start = datetime.combine(first, datetime.min.time(), tzinfo=config.MANILA)
    return start, datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=config.MANILA)


async def fetch_trips(registration: str, first: date, last: date, *, counter: CartrackCallCounter | None = None, limit: int | None = None) -> list[dict]:
    """All trips of one truck whose window overlaps [first, last] (Manila), paged with limit=100."""
    start, end = day_bounds(first, last)
    trips: list[dict] = []
    page = 1
    while True:
        payload = await cartrack_client.get_json(
            f"/rest/trips/{registration}",
            {"start_timestamp": _stamp(start), "end_timestamp": _stamp(end), "limit": limit or config.TRIPS_PAGE_LIMIT, "page": page},
            counter=counter,
        )
        data = payload.get("data") or []
        trips.extend(data)
        last_page = int((payload.get("meta") or {}).get("last_page") or 1)
        if not data or page >= last_page:
            unique, repeated = daily.dedupe_trips(trips)
            if repeated:
                logger.warning("[FLEET_HEALTH] %s: dropped %d repeated trip rows (same trip_id on several pages)", registration, repeated)
                if counter is not None:
                    counter.trip_duplicates = getattr(counter, "trip_duplicates", 0) + repeated
            return unique
        page += 1


def load_assignments(db: Session, plates: list[str], first: date, last: date) -> dict[tuple[str, date], list[dict]]:
    """ONE Neon query: those trucks' sales orders over the date range from the saved history (sales_orders) -
    who drove and where they delivered. Grouped by (plate, day). The connection is released right after, so it
    is never held open while the job waits on Cartrack or geocoding."""
    grouped: dict[tuple[str, date], list[dict]] = {}
    if plates:
        rows = db.execute(
            select(SalesOrderHistory.vehicle_id, SalesOrderHistory.expected_shipment_date, SalesOrderHistory.driver_id, SalesOrderHistory.shipping_address)
            .where(SalesOrderHistory.vehicle_id.in_(plates), SalesOrderHistory.expected_shipment_date >= first, SalesOrderHistory.expected_shipment_date <= last)
        ).all()
        for plate, day, driver_id, address in rows:
            grouped.setdefault((plate, day), []).append({"driver_id": driver_id, "shipping_address": address})
    db.rollback()  # end the read transaction; the writes below start on a fresh connection
    return grouped


async def delivery_points(assignments: list[dict], *, geocode: bool = True) -> tuple[list[tuple[float, float]], int]:
    """Geocode the assigned SOs' delivery addresses (memory-cached). Returns (points, unresolved_count)."""
    if not geocode:
        return [], 0
    from services import google_maps
    from services.sales_order_location import address_lines

    points: list[tuple[float, float]] = []
    unresolved = 0
    for assignment in assignments:
        text = ", ".join(address_lines(assignment.get("shipping_address")))
        if not text:
            unresolved += 1
            continue
        if text not in _geocode_cache:
            try:
                found = await google_maps.geocode_address(text)
                _geocode_cache[text] = (found["lat"], found["lng"]) if found else None
            except Exception:  # noqa: BLE001 - an unresolved address only lowers idle-at-stop accuracy
                _geocode_cache[text] = None
        if _geocode_cache[text]:
            points.append(_geocode_cache[text])
        else:
            unresolved += 1
    return points, unresolved


def upsert_row(db: Session, vehicle_id: int, day: date, values: dict) -> str:
    """Insert or overwrite the single (vehicle_id, stat_date) row. Idempotent."""
    existing = db.execute(select(VehicleDailyStat).where(VehicleDailyStat.vehicle_id == vehicle_id, VehicleDailyStat.stat_date == day)).scalar_one_or_none()
    if existing is None:
        db.add(VehicleDailyStat(vehicle_id=vehicle_id, stat_date=day, computed_at=datetime.now(timezone.utc), **values))
        return "inserted"
    for key, value in values.items():
        setattr(existing, key, value)
    existing.computed_at = datetime.now(timezone.utc)
    return "updated"


def _vehicle_info(entry: dict) -> dict:
    return {"id": entry["vehicle_id"], "plate": entry["plate"], "plate_key": entry["plate_key"], "fuel_capacity_l": entry["fuel_capacity_l"]}


async def run_nightly(db: Session, day: date | None = None, *, dry_run: bool = False, geocode: bool = True) -> dict:
    """Compute (and, unless dry_run, upsert) the previous Manila day for every tracked truck."""
    counter = CartrackCallCounter()
    day = day or (datetime.now(config.MANILA).date() - timedelta(days=1))
    if _run_lock.locked():
        return {"skipped": "another fleet-health job is running", "day": day.isoformat()}
    async with _run_lock:
        fleet = await match_fleet(db, counter=counter)
        assigned = load_assignments(db, [e["plate"] for e in fleet["tracked"]], day, day)
        rows, pending = [], []
        for entry in fleet["tracked"]:
            trips = await fetch_trips(entry["registration"], day, day, counter=counter)
            assignments = assigned.get((entry["plate"], day), [])
            points, unresolved = await delivery_points(assignments, geocode=geocode)
            samples = sampler.samples_for(entry["plate_key"])
            values = daily.compute_daily_row(vehicle=_vehicle_info(entry), day=day, trips=trips, samples=samples, sites=[*config.WAREHOUSE_SITES.values(), *points], assignments=assignments, unresolved_delivery_points=unresolved)
            if values["data_quality"].get("status_samples", 0) == 0 and sampler.started_at().astimezone(config.MANILA).date() > day:
                values["data_quality"]["sampler_started_after_day"] = True
            rows.append({"vehicle": entry["plate"], "stat_date": day.isoformat(), **values})
            pending.append((entry, values))
        outcomes = {}
        if not dry_run:
            for entry, values in pending:
                outcomes[entry["plate"]] = upsert_row(db, entry["vehicle_id"], day, values)
            db.flush()
            for entry, _values in pending:  # battery / fuel issues; a failure here never costs the day's stats
                try:
                    with db.begin_nested():
                        automation.evaluate_vehicle(db, vehicle_id=entry["vehicle_id"], plate=entry["plate"], day=day, capacity_unconfirmed=entry["plate_key"] in config.UNCONFIRMED_FUEL_CAPACITY_PLATES)
                except Exception:  # noqa: BLE001
                    logger.exception("[FLEET_HEALTH] issue detection failed for %s", entry["plate"])
            db.commit()
            snapshot.invalidate()
    logger.info("[FLEET_HEALTH] nightly %s dry_run=%s vehicles=%d cartrack_calls=%d", day, dry_run, len(rows), counter.calls)
    return {"day": day.isoformat(), "dry_run": dry_run, "rows": rows, "written": outcomes, "cartrack_calls": counter.calls, "cartrack_calls_by_path": counter.by_path, "trip_duplicates_dropped": getattr(counter, "trip_duplicates", 0)}


async def run_backfill(db: Session, days: int = config.BACKFILL_DAYS_DEFAULT, *, dry_run: bool = True, geocode: bool = True, limit: int | None = None, end: date | None = None) -> dict:
    """Last `days` complete Manila days (today excluded) from /rest/trips only. There are no historical status samples,
    so fuel and battery stay NULL (data_quality.no_status_samples)."""
    counter = CartrackCallCounter()
    last = end or (datetime.now(config.MANILA).date() - timedelta(days=1))
    if last >= datetime.now(config.MANILA).date():
        raise ValueError("the backfill covers complete days only: end must be before today (Manila)")
    first = last - timedelta(days=days - 1)
    if _run_lock.locked():
        return {"skipped": "another fleet-health job is running"}
    async with _run_lock:
        fleet = await match_fleet(db, counter=counter)
        assigned = load_assignments(db, [e["plate"] for e in fleet["tracked"]], first, last)
        rows, pending, trip_totals = [], [], {}
        for entry in fleet["tracked"]:
            trips = await fetch_trips(entry["registration"], first, last, counter=counter, limit=limit)
            trip_totals[entry["plate"]] = len(trips)
            for offset in range(days):
                day = first + timedelta(days=offset)
                assignments = assigned.get((entry["plate"], day), [])
                points, unresolved = await delivery_points(assignments, geocode=geocode)
                values = daily.compute_daily_row(vehicle=_vehicle_info(entry), day=day, trips=trips, samples=None, sites=[*config.WAREHOUSE_SITES.values(), *points], assignments=assignments, unresolved_delivery_points=unresolved)
                values["data_quality"]["backfill"] = True
                rows.append({"vehicle": entry["plate"], "stat_date": day.isoformat(), **values})
                pending.append((entry, day, values))
        outcomes = {}
        if not dry_run:
            for entry, day, values in pending:
                outcomes[f"{entry['plate']} {day}"] = upsert_row(db, entry["vehicle_id"], day, values)
            db.commit()
            snapshot.invalidate()
    logger.info("[FLEET_HEALTH] backfill %s..%s dry_run=%s rows=%d cartrack_calls=%d", first, last, dry_run, len(rows), counter.calls)
    return {"from": first.isoformat(), "to": last.isoformat(), "dry_run": dry_run, "rows": rows, "rows_total": len(rows), "trips_fetched": trip_totals, "written": len(outcomes), "cartrack_calls": counter.calls, "cartrack_calls_by_path": counter.by_path, "page_limit": limit or config.TRIPS_PAGE_LIMIT, "trip_duplicates_dropped": getattr(counter, "trip_duplicates", 0)}
