from __future__ import annotations

import math
import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from datetime import date as date_type

from auth.dependencies import require_role
from database import get_db, SessionLocal
from models.vehicle import Vehicle
from models.inventory import SalesOrderCache
from models.sales_order_history import SalesOrderHistory
from services import branches as branch_service
from models.sales_order_lines import SalesOrderLine
from services import live_gps_store, live_sales_order_cache, memory_tables, transfer_orders
from services.sales_order_history_sync import sync_history_row
from services.serialize import row_to_dict
from services.ws_manager import manager
from services.sales_order_location import shipping_city
from services.item_weight import calculate_line_weight_kg
from services.delivery_status import is_delivered, sales_order_delivery_status
from services.zoho_client import ZohoError
from services import zoho_acquisition, zoho_usage

READ_ROLES = Depends(require_role("admin", "dispatcher", "warehouse"))
WRITE_ROLES = Depends(require_role("admin", "dispatcher"))
router = APIRouter(tags=["fleet"])
logger = logging.getLogger("fleet_refresh")
OPS_TZ = ZoneInfo("Asia/Manila")
_sync_lock = threading.Lock()
# A plain `_fleet_cache is None` check previously treated an empty-but-built cache ([])
# as "already built forever" (None is falsy but so is []), so a Fleet rebuilt with zero
# vehicles (e.g. right after a DB wipe) never picked up vehicles added afterward without
# a restart or an explicit /vehicles/refresh. _fleet_cache_built is the real signal now.
_fleet_cache: list[dict] = []
_fleet_cache_built = False
# Single-flight build: at most ONE thread rebuilds the Fleet cache (and so talks to Zoho) at a time, however many
# /vehicles requests arrive during a cold start or after an invalidation. _build_generation counts finished builds
# so a request that waited on the lock reuses that build instead of starting another. _stale_fleet_cache is the
# last fully built snapshot, kept across invalidation so readers that arrive mid-rebuild are served it, not blocked.
ASSIGNED_ONLY = frozenset({"assigned"})  # the only assignment state Fleet displays
_build_lock = threading.Lock()
_build_generation = 0
_stale_fleet_cache: list[dict] = []
OPERATIONAL_CAPACITY_KG = {"DCD8953": 1800.0, "DCD8954": 1800.0, "DCD8955": 1800.0, "NFX5791": 1000.0}

LOCKED_VEHICLES = {
    "NAN9911": "Assigned to Production",
    "NAJ6018": "For Repair",
}


def invalidate_fleet_cache() -> None:
    """Call on every assignment mutation (assign, unassign, edit, release/complete,
    vehicle lock/unlock) and on every vehicle add/edit, so the Fleet tab never shows a
    stale snapshot - including the "0 vehicles" snapshot right after a DB wipe."""
    global _fleet_cache, _fleet_cache_built
    _fleet_cache = []
    _fleet_cache_built = False


def _fleet_status(plate: str) -> str:
    live = live_gps_store.get(plate)
    if live is None:
        return "No signal"
    return live["status"]


def _vehicle_payload(vehicle: Vehicle, db: Session, fresh_orders: dict[str, dict] | None = None, sync_errors: list[str] | None = None, assigned_by_vehicle: dict[str, list[SalesOrderCache]] | None = None) -> dict:
    payload = row_to_dict(vehicle)
    plate = (vehicle.plate_no or "").strip().upper()
    assigned = (assigned_by_vehicle or {}).get(plate, [])
    if assigned_by_vehicle is None:
        assigned = [o for o in live_sales_order_cache.get_assigned_snapshot() if o.vehicle_id == plate and o.assignment_status == "assigned"]
    capacity = OPERATIONAL_CAPACITY_KG.get(plate) or (float(vehicle.rated_capacity_kg) if vehicle.rated_capacity_kg is not None else None)
    def line_weight(item: dict) -> float | None:
        return calculate_line_weight_kg(item.get("quantity"), item.get("unit") or item.get("unit_name"), item.get("item_id") or item.get("itemid"), item=item, context="fleet")
    def order_metrics(o, raw=None):
        raw = raw or o.raw_json or {}
        items = raw.get("line_items") or []
        line_weights = [line_weight(i) for i in items]
        valid = [(i, weight) for i, weight in zip(items, line_weights) if weight is not None]
        if not valid:
            return 0.0, 0.0
        ordered = sum(weight for _, weight in valid)
        shipped = sum(weight if is_delivered(raw) else weight * min(1, float(i.get("quantity_shipped") or 0) / float(i.get("quantity") or 1)) for i, weight in valid)
        return ordered, shipped
    order_raw = lambda o: (fresh_orders or {}).get(str(o.id), o.raw_json or {})
    metrics = [order_metrics(o, order_raw(o)) for o in assigned]
    delivery_statuses = [sales_order_delivery_status(order_raw(o)) for o in assigned]
    total_weight = sum(item[0] or 0 for item in metrics)
    delivered_weight = sum(metric[0] or 0 for metric, order in zip(metrics, assigned) if is_delivered(order_raw(order)))
    assigned_weight = max(0, total_weight - delivered_weight)
    shipped_weight = delivered_weight
    delivered_count = sum(status == "Delivered" for status in delivery_statuses)
    def warehouses_for(order) -> list[str]:
        raw = order.raw_json or {}
        names = []
        for item in raw.get("line_items") or []:
            location_obj = item.get("location") if isinstance(item.get("location"), dict) else {}
            warehouse_obj = item.get("warehouse") if isinstance(item.get("warehouse"), dict) else {}
            location = item.get("location_name") or item.get("warehouse_name") or location_obj.get("location_name") or warehouse_obj.get("warehouse_name")
            if location: names.append(str(location).strip())
        location_obj = raw.get("location") if isinstance(raw.get("location"), dict) else {}
        warehouse_obj = raw.get("warehouse") if isinstance(raw.get("warehouse"), dict) else {}
        top = raw.get("location_name") or raw.get("warehouse_name") or (raw.get("warehouse") if isinstance(raw.get("warehouse"), str) else None) or location_obj.get("location_name") or warehouse_obj.get("warehouse_name")
        if top: names.append(str(top).strip())
        return list(dict.fromkeys(name for name in names if name))
    associated = [{"soNumber": o.salesorder_number or o.id, "orderType": transfer_orders.order_type_of(o), "clientName": o.customer_name, "destinationCity": shipping_city(o), "warehouse": ", ".join(warehouses_for(o)) or None, "orderedWeightKg": order_metrics(o, order_raw(o))[0], "shippedWeightKg": order_metrics(o, order_raw(o))[1], "remainingWeightKg": max(0, (order_metrics(o, order_raw(o))[0] or 0) - (order_metrics(o, order_raw(o))[1] or 0)), "status": o.order_status, "shipmentStatus": o.shipment_status, "deliveryStatus": sales_order_delivery_status(order_raw(o))} for o in assigned]
    warehouses = sorted({str(item["warehouse"]).strip() for item in associated if item["warehouse"]})
    unavailable = plate in LOCKED_VEHICLES or not assigned
    fulfillment_status = "none" if not assigned else ("fulfilled" if delivered_count == len(assigned) else "partial" if delivered_count else "pending")
    payload.update({
        "associated_sos": [] if plate in LOCKED_VEHICLES else associated,
        "warehouse_pickup": None if unavailable else ", ".join(warehouses) or None,
        "locked": plate in LOCKED_VEHICLES,
        "lock_reason": LOCKED_VEHICLES.get(plate),
        "capacity_kg": None if plate in LOCKED_VEHICLES else capacity,
        "load": None if unavailable or capacity is None else {"assignedWeightKg": assigned_weight, "capacityKg": capacity, "remainingWeightKg": max(0, capacity - assigned_weight), "utilizationPercent": min(100, assigned_weight / capacity * 100)},
        "fulfillment": None if unavailable or not assigned else {"assignedWeightKg": total_weight, "shippedWeightKg": shipped_weight, "remainingWeightKg": assigned_weight, "percent": delivered_weight / total_weight * 100 if total_weight else 0, "deliveredOrders": delivered_count, "totalOrders": len(assigned), "status": fulfillment_status},
        "zohoSyncErrors": sync_errors or [],
    })
    return payload


def _merge_live(payload: dict) -> dict:
    """Merge the in-memory GPS live store into a (possibly cached) Fleet payload at
    response time. GPS never touches the DB, so this happens on every request, cached
    or not - the cached part is only the Zoho/assignment data."""
    plate = str(payload.get("plate_no") or "").strip().upper()
    live = live_gps_store.get(plate)
    merged = dict(payload)
    merged.update({
        "status": _fleet_status(plate),
        "current_lat": live["lat"] if live else None,
        "current_lng": live["lng"] if live else None,
        "heading": live["heading"] if live else None,
        "speed_kph": live["speed_kph"] if live else None,
        "fuel_pct": live["fuel_pct"] if live else None,
        "ignition_on": live["ignition_on"] if live else None,
        "zone": live["zone"] if live else None,
        "gps_time": live.get("gps_time") if live else None,  # the device's own timestamp for this fix (UTC ISO)
        "last_updated": live["last_updated"].isoformat() if live else None,
    })
    return merged


def _operational_date(order: SalesOrderCache) -> date_type | None:
    """The day an order belongs to in Fleet: its expected delivery (shipment) date.

    Assigning on the 2nd for delivery on the 5th must show on the 5th, not the 2nd, and must
    not touch the load or fulfillment of any other day. The assignment date is only a
    fallback for rows that have no expected shipment date at all.
    """
    expected = getattr(order, "expected_shipment_date", None)
    if expected:
        return expected
    assigned_at = getattr(order, "assigned_at", None)
    if assigned_at:
        if assigned_at.tzinfo is None:
            assigned_at = assigned_at.replace(tzinfo=timezone.utc)
        return assigned_at.astimezone(OPS_TZ).date()
    return None


def _historical_vehicle_payload(vehicle: Vehicle, rows: list[SalesOrderHistory], db: Session) -> dict:
    """Build a read-only Fleet snapshot from the durable historical tables.

    Historical rows do not have live ``raw_json``.  They must therefore not be sent
    through ``_vehicle_payload`` (which is deliberately a live Zoho calculator).
    """
    payload = row_to_dict(vehicle)
    plate = (vehicle.plate_no or "").strip().upper()
    by_order = {str(row.id): row for row in rows}
    line_rows = db.execute(select(SalesOrderLine).where(SalesOrderLine.sales_order_id.in_(list(by_order) or ["__none__"]))).scalars().all()
    lines_by_order: dict[str, list[SalesOrderLine]] = {}
    for line in line_rows:
        lines_by_order.setdefault(str(line.sales_order_id), []).append(line)

    associated = []
    total_weight = 0.0
    shipped_weight = 0.0
    delivered_count = 0
    warehouses: set[str] = set()
    for row in rows:
        order_lines = lines_by_order.get(str(row.id), [])
        ordered = sum(float(line.weight_kg or 0) for line in order_lines)
        shipped = sum(float(line.weight_kg or 0) * min(1.0, float(line.quantity_shipped or 0) / float(line.quantity or 1)) for line in order_lines)
        delivery_status = str(row.delivery_status or "Unknown").strip().casefold()
        if delivery_status == "delivered":
            shipped = ordered
            delivered_count += 1
        total_weight += ordered
        shipped_weight += shipped
        names = {line.location_name.strip() for line in order_lines if line.location_name and line.location_name.strip()}
        warehouses.update(names)
        associated.append({
            "soNumber": row.salesorder_number or row.id,
            "orderType": transfer_orders.order_type_of(row),
            "clientName": row.customer_name,
            "destinationCity": shipping_city(row),
            "warehouse": ", ".join(sorted(names)) or None,
            "orderedWeightKg": ordered,
            "shippedWeightKg": shipped,
            "remainingWeightKg": max(0, ordered - shipped),
            "status": row.order_status,
            "shipmentStatus": row.shipment_status,
            "deliveryStatus": "Delivered" if delivery_status == "delivered" else row.delivery_status or "Unknown",
        })
    capacity = OPERATIONAL_CAPACITY_KG.get(plate) or (float(vehicle.rated_capacity_kg) if vehicle.rated_capacity_kg is not None else None)
    payload.update({
        "associated_sos": associated,
        "warehouse_pickup": ", ".join(sorted(warehouses)) or None,
        "locked": plate in LOCKED_VEHICLES,
        "lock_reason": LOCKED_VEHICLES.get(plate),
        "capacity_kg": capacity,
        "load": {"assignedWeightKg": total_weight, "capacityKg": capacity, "remainingWeightKg": max(0, capacity - total_weight), "utilizationPercent": min(100, total_weight / capacity * 100)} if capacity and associated else None,
        "fulfillment": {"assignedWeightKg": total_weight, "shippedWeightKg": shipped_weight, "remainingWeightKg": max(0, total_weight - shipped_weight), "percent": shipped_weight / total_weight * 100 if total_weight else 0, "deliveredOrders": delivered_count, "totalOrders": len(associated), "status": "fulfilled" if delivered_count == len(associated) else "partial" if delivered_count else "pending"} if associated else None,
        "zohoSyncErrors": [],
    })
    return payload

# Role gates (2026-10-12): the Catalyst port left these routes open. Reads: admin, dispatcher (planner is aliased to
# dispatcher), warehouse. Refresh / manual position: admin, dispatcher. Drivers, helpers, officers and portal clients
# are refused. /ws/fleet (websocket) is NOT covered by these dependencies.


@router.get("/vehicles", dependencies=[READ_ROLES])
def list_vehicles(status: str | None = None, date: str | None = None, db: Session = Depends(get_db)):
    requested_date = date_type.fromisoformat(date) if date else None
    if date:
        # Past-date Fleet view: Neon history only. Today remains the live operational view.
        target = requested_date
        if target >= datetime.now(OPS_TZ).date():
            date = None
        else:
            vehicles = db.execute(select(Vehicle)).scalars().all()
            history_rows = branch_service.filter_allowed(db.execute(select(SalesOrderHistory)).scalars().all())  # RGF/MSSI only
            history_rows = [row for row in history_rows if (_operational_date(row) or row.expected_shipment_date) == target]
            grouped: dict[str, list[SalesOrderHistory]] = {}
            for row in history_rows:
                if row.vehicle_id:
                    grouped.setdefault(str(row.vehicle_id).upper(), []).append(row)
            result = [_merge_live(_historical_vehicle_payload(v, grouped.get(str(v.plate_no or "").upper(), []), db)) for v in vehicles]
            if status:
                result = [v for v in result if v["status"] == status]
            return result
    if requested_date is not None:
        # A selected current or future date is date-scoped by expected delivery date: only that
        # day's assigned orders feed each truck's list, load and fulfillment.
        vehicles = db.execute(select(Vehicle)).scalars().all()
        # Assigned SOs only: no 16-day window pull, and no Zoho call for completed/released history.
        snapshot, had_failures = live_sales_order_cache.get_assigned_snapshot_ex(include_window=False, statuses=ASSIGNED_ONLY)
        selected = [o for o in snapshot if o.assignment_status == "assigned" and _operational_date(o) == requested_date]
        grouped: dict[str, list[SalesOrderCache]] = {}
        for row in selected:
            grouped.setdefault(str(row.vehicle_id or "").upper(), []).append(row)
        logger.info("[FLEET_DATE_SCOPE] requested_date=%s vehicle_assignments=%s", requested_date, {plate: [o.salesorder_number or o.id for o in orders] for plate, orders in grouped.items()})
        result = [_merge_live(_vehicle_payload(v, db, assigned_by_vehicle=grouped)) for v in vehicles]
        if status:
            result = [v for v in result if v["status"] == status]
        return result
    if not date:
        # Current/today view uses only active in-memory assignments and live telemetry.
        pass

    logger.info("[FLEET_REFRESH] Starting refresh")
    if not _fleet_cache_built:
        generation = _build_generation
        if _build_lock.acquire(blocking=False):
            try:
                if not _fleet_cache_built and generation == _build_generation:
                    _build_fleet_cache(db)
            finally:
                _build_lock.release()
        elif not _stale_fleet_cache:
            # Cold start with nothing to serve yet: wait for the in-flight build and reuse its result.
            with _build_lock:
                if not _fleet_cache_built and generation == _build_generation:
                    _build_fleet_cache(db)
    # Mid-rebuild readers (another thread holds the build lock) get the last good snapshot.
    serving = _fleet_cache if (_fleet_cache_built or not _stale_fleet_cache or not _build_lock.locked()) else _stale_fleet_cache
    result = [_merge_live(v) for v in serving]
    if status:
        result = [v for v in result if v["status"] == status]
    logger.info("[FLEET_REFRESH] Vehicles: %s; Associations resolved: %s; Load and fulfillment calculations completed", len(result), sum(len(v["associated_sos"]) for v in result))
    return result


def _build_fleet_cache(db: Session) -> None:
    """Rebuild the Fleet cache from the assigned SOs. Caller holds _build_lock. Only the assigned SOs are fetched from
    Zoho (one detail call each, none for ones already in memory) - never the whole shipment window."""
    global _fleet_cache, _fleet_cache_built, _stale_fleet_cache, _build_generation
    vehicles = db.execute(select(Vehicle)).scalars().all()
    plates = {str(v.plate_no or "").strip().upper() for v in vehicles}
    if live_sales_order_cache.local_zoho_warmup_disabled():
        snapshot, had_failures = live_sales_order_cache.cached_assigned_records(), False  # memory only: no Zoho at all
    else:
        snapshot, had_failures = live_sales_order_cache.get_assigned_snapshot_ex(include_window=False, statuses=ASSIGNED_ONLY)
    assigned_rows = [o for o in snapshot if o.assignment_status == "assigned" and o.vehicle_id in plates]
    grouped: dict[str, list[SalesOrderCache]] = {}
    for row in assigned_rows: grouped.setdefault(str(row.vehicle_id or "").upper(), []).append(row)
    built = [_vehicle_payload(v, db, assigned_by_vehicle=grouped) for v in vehicles]
    _fleet_cache = built
    # Only latch the cache when every assigned SO's Zoho detail came back clean. Right
    # after a restart, _assigned_zoho starts empty and every assigned SO must be
    # fetched at once - if any of those fetches failed (Zoho rate limit/blip), latching
    # here would leave that vehicle's Associated SOs/Load/Fulfillment stuck on "-" until
    # someone clicks Refresh. Leaving _fleet_cache_built False makes the very next
    # request retry instead, so it self-heals within one poll cycle.
    _fleet_cache_built = not had_failures
    if not had_failures:
        _stale_fleet_cache = built
    _build_generation += 1
    if had_failures:
        logger.warning("[FLEET_REFRESH] One or more assigned SOs failed to fetch from Zoho this cycle; not latching cache, will retry next request")


def warm_fleet_cache() -> None:
    """Build the Fleet cache once at startup, in the background, so the first person to open Fleet gets it instantly.
    Counted as background fleet_sync usage; shares the single-flight lock with /vehicles."""
    if live_sales_order_cache.local_zoho_warmup_disabled():
        logger.info("[FLEET_REFRESH] Startup Fleet cache warm-up skipped: LOCAL_DISABLE_ZOHO_WARMUP")
        return
    try:
        with zoho_usage.labelled("fleet_sync"), SessionLocal() as db, _build_lock:
            if not _fleet_cache_built:
                _build_fleet_cache(db)
        logger.info("[FLEET_REFRESH] Startup Fleet cache build finished (latched=%s)", _fleet_cache_built)
    except Exception:
        logger.exception("[FLEET_REFRESH] Startup Fleet cache build failed; the first /vehicles request will build it")


@router.post("/vehicles/refresh", dependencies=[WRITE_ROLES])
@zoho_acquisition.operation("fleet-refresh", reuse_details=True)
def refresh_vehicles(force: bool = False, db: Session = Depends(get_db)):
    """Recompute Fleet from live Zoho data for currently-assigned SOs only (never a bulk
    sync of every SO). `force` is accepted for API compatibility (the 60-min scheduled sync
    and the manual Refresh button both call this) but every call already re-fetches Zoho
    for the assigned set and only writes sales_order_history rows that actually changed -
    there is no "skip because still fresh" path to force past.

    This is idempotent: no stored load is decremented; every value is rebuilt
    from the assigned orders and their delivery statuses.
    """
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(409, "Fleet refresh already in progress")
    started = time.monotonic()
    try:
        live_sales_order_cache.refresh_shared_window()
        assigned = [o for o in live_sales_order_cache.get_assigned_snapshot() if o.assignment_status == "assigned"]
        fresh_orders: dict[str, dict] = {}
        sync_errors: list[str] = []
        request_cache: dict[str, dict] = {}
        for order in assigned:
            raw = order.raw_json or {}
            zoho_id = raw.get("salesorder_id") or raw.get("sales_order_id") or raw.get("transfer_order_id") or raw.get("purchaseorder_id") or raw.get("id")
            if not zoho_id:
                sync_errors.append(str(order.salesorder_number or order.id))
                continue
            zoho_id = str(zoho_id)
            if zoho_id not in request_cache:
                request_cache[zoho_id] = raw
            fresh_orders[str(order.id)] = request_cache[zoho_id]
        # Persist successful Zoho snapshots and release a whole truck batch only
        # when every active SO is delivered. This preserves history while removing
        # completed work from the active Fleet load.
        for order in assigned:
            fresh = fresh_orders.get(str(order.id))
            if fresh:
            # Do not erase locally cached identifiers/metadata when Zoho omits a
            # field in a detail response; the newest non-empty value wins.
                live_sales_order_cache.merge_zoho_payload(order, fresh)
                order.synced_at = datetime.now(timezone.utc)
        # A truck's batch is one delivery day: finishing the 3rd must not wait on the 5th.
        by_vehicle: dict[tuple[str, object], list[SalesOrderCache]] = {}
        for order in assigned:
            by_vehicle.setdefault((str(order.vehicle_id or ""), _operational_date(order)), []).append(order)
        for orders in by_vehicle.values():
            if orders and all(sales_order_delivery_status(fresh_orders.get(str(order.id), order.raw_json or {})) == "Delivered" for order in orders):
                completed_at = datetime.now(timezone.utc)
                for order in orders:
                    order.assignment_status = "completed"
                    order.completed_at = completed_at
                    live_sales_order_cache.set_assignment(order.id, assignment_status="completed", completed_at=completed_at)
        # Write-on-event to sales_order_history: only orders whose Zoho snapshot actually
        # changed this cycle, or that just completed - never every assigned order every cycle.
        for order in assigned:
            if str(order.id) in fresh_orders or order.assignment_status == "completed":
                sync_history_row(db, order)
        db.commit()
        vehicles = db.execute(select(Vehicle)).scalars().all()
        logger.info("[FLEET_SYNC] trucks_processed=%d sos_released=%d unresolved_count=%d duration_ms=%d", len(vehicles), sum(1 for order in assigned if order.assignment_status == "completed"), sum(1 for order in assigned if sales_order_delivery_status(order.raw_json or {}) == "Unresolved"), int((time.monotonic() - started) * 1000))
        global _fleet_cache, _fleet_cache_built, _stale_fleet_cache
        _fleet_cache = [_vehicle_payload(v, db, sync_errors=sync_errors) for v in vehicles]
        _fleet_cache_built = True
        _stale_fleet_cache = _fleet_cache
        return [_merge_live(v) for v in _fleet_cache]
    finally:
        _sync_lock.release()


def scheduled_fleet_sync() -> None:
    """Run the same authoritative Zoho reconciliation without a browser open.

    Skipped (zero Zoho calls) outside working hours or when no SO is currently assigned:
    there is nothing for the sync to reconcile, and a post-restart run would otherwise
    re-list and re-hydrate the whole shared window for no benefit."""
    if live_sales_order_cache.local_zoho_warmup_disabled():
        logger.info("[FLEET_REFRESH] Scheduled sync skipped: LOCAL_DISABLE_ZOHO_WARMUP")
        return
    if not zoho_usage.is_working_time():
        logger.info("[FLEET_REFRESH] Scheduled sync skipped: outside working hours")
        return
    if not live_sales_order_cache.has_active_assignments():
        logger.info("[FLEET_REFRESH] Scheduled sync skipped: no assigned SOs")
        return
    try:
        with zoho_usage.labelled("fleet_sync"), SessionLocal() as db:
            refresh_vehicles(force=True, db=db)
        logger.info("[FLEET_REFRESH] Scheduled Zoho fleet sync completed")
    except Exception:
        logger.exception("[FLEET_REFRESH] Scheduled Zoho fleet sync failed")


@router.get("/vehicles/{vehicle_id}", dependencies=[READ_ROLES])
def get_vehicle(vehicle_id: int, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise HTTPException(404, "Vehicle not found")
    return _merge_live(_vehicle_payload(vehicle, db))


@router.get("/vehicles/{vehicle_id}/trail", dependencies=[READ_ROLES])
def get_vehicle_trail(vehicle_id: int, limit: int = 50):
    """GPS history is not persisted (see migration b7e2c9a4d1f6) - live position only,
    from the in-memory store / WebSocket."""
    return []


@router.get("/geofences", dependencies=[READ_ROLES])
def list_geofences():
    # 2026-09-24 (Step 6): geofences dropped from Neon - nothing ever wrote a row.
    return memory_tables.GEOFENCES


@router.get("/drivers", dependencies=[READ_ROLES])
def list_drivers():
    # 2026-09-24 (Step 6): drivers dropped from Neon - nothing ever wrote a row.
    return memory_tables.DRIVERS


class PositionUpdateBody(BaseModel):
    lat: float
    lng: float
    heading: float
    speedKph: float
    skipTrail: bool = False


@router.post("/vehicles/{vehicle_id}/position", dependencies=[WRITE_ROLES])
async def post_vehicle_position(vehicle_id: int, body: PositionUpdateBody, db: Session = Depends(get_db)):
    """Manual/testing entry point into the same in-memory live GPS store the Cartrack
    poller writes to. No DB session is used for the GPS write - only to resolve the
    plate_no this vehicle_id maps to."""
    for value in (body.lat, body.lng, body.heading, body.speedKph):
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise HTTPException(400, "lat, lng, heading and speedKph must all be numbers")

    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise HTTPException(404, "Vehicle not found")
    plate = str(vehicle.plate_no or "").strip().upper()

    entry = live_gps_store.upsert(
        plate,
        lat=body.lat,
        lng=body.lng,
        heading=body.heading,
        speed_kph=body.speedKph,
        fuel_pct=None,
        ignition_on=None,
    )
    await manager.broadcast(
        {
            "type": "VEHICLE_POSITION_UPDATE",
            "vehicle_id": vehicle_id,
            "plate_no": vehicle.plate_no,
            "current_lat": entry["lat"],
            "current_lng": entry["lng"],
            "heading": entry["heading"],
            "speed_kph": entry["speed_kph"],
            "ignition_on": entry["ignition_on"],
            "fuel_pct": entry["fuel_pct"],
            "status": entry["status"],
            "address": entry["address"] or "",
            "last_updated": entry["last_updated"].isoformat(),
        }
    )
    return _merge_live(_vehicle_payload(vehicle, db))
