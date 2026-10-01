from __future__ import annotations

import logging
import threading
import time
import contextvars
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as date_type, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, get_current_user, require_role
from database import get_db
from database import db_egress_stats
from services import staff_directory_cache, live_sales_order_cache, fleet_static_cache
from services import zoho_client
from services import zoho_acquisition
from services.sales_order_history_sync import sync_history_row
from models.dispatch_pipeline import WarehouseLoadingChecklist
from models.vehicle import Vehicle
from models.inventory import SalesOrderCache
from models.route import LoadManifest, ManifestItem
from models.sales_order_history import SalesOrderHistory
from services.serialize import row_to_dict
from routers.dispatch import SendMessageBody, send_message
from routers.fleet import invalidate_fleet_cache
from routers.assignment import _invalidate_assignment_options_cache
from services.item_weight import calculate_order_weight_kg

router = APIRouter(prefix="/api/load-planning", tags=["dispatch-pipeline"])
OPS = require_role("admin", "dispatcher", "warehouse")
PHT = ZoneInfo("Asia/Manila")
logger = logging.getLogger("dispatch_dashboard")
_dashboard_last_good: dict[str, dict] = {}
_zoho_date_cache: dict[str, tuple[float, dict[str, dict], dict]] = {}
_zoho_detail_cache: dict[str, tuple[str, dict]] = {}
_zoho_cache_lock = threading.Lock()

def _zoho_date_records(target: date_type, force: bool) -> tuple[dict[str, dict], dict]:
    epoch = zoho_acquisition.generation()
    key = target.isoformat()
    with _zoho_cache_lock:
        cached = _zoho_date_cache.get(key)
        if cached and not force and time.monotonic() - cached[0] < 60:
            return dict(cached[1]), {**cached[2], "cache": "warm"}
    records: dict[str, dict] = {}
    rows_before_filter = 0
    rows_after_filter = 0
    pages = 0
    page = 1
    while True:
        body = zoho_client.fetch_sales_orders_by_shipment_date(target, target, page=page, per_page=200)
        pages += 1
        batch = body.get("salesorders") or []
        rows_before_filter += len(batch)
        for record in batch:
            oid = str(record.get("salesorder_id") or record.get("id") or "")
            if oid and str(record.get("shipment_date") or record.get("expected_shipment_date") or "")[:10] == key:
                records[oid] = record
                rows_after_filter += 1
        context = body.get("page_context") or {}
        more = context.get("has_more_page") is True or str(context.get("has_more_page")).lower() == "true"
        if not batch or not more: break
        page += 1
    stats = {"pages_fetched": pages, "shipment_filter": "shipment_date_start/shipment_date_end", "cache": "cold" if force or cached is None else "expired", "rows_before_filter": rows_before_filter, "rows_after_filter": rows_after_filter}
    with zoho_acquisition.publication(epoch) as current:
        if current:
            with _zoho_cache_lock:
                _zoho_date_cache[key] = (time.monotonic(), dict(records), dict(stats))
    return records, stats

def _clean_text(value: object) -> str:
    text = str(value or "").strip()
    for _ in range(3):
        try:
            fixed = text.encode("latin1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if fixed == text:
            break
        text = fixed
    return text

def _city(value: object) -> str:
    text = _clean_text(value)
    words = text.split()
    if words and words[-1].casefold() == "city":
        words = words[:-1]
    return (" ".join(words).title() + " City") if words else "Unassigned"


def _weight(order: SalesOrderCache) -> float:
    weight = calculate_order_weight_kg(order)
    # Do not block assignment/manifest operations when Zoho package metadata is
    # temporarily unavailable. Unknown weight contributes zero until refreshed.
    return weight if weight is not None else 0.0


def _cold_chain(order: SalesOrderCache) -> str:
    text = " ".join(str((order.raw_json or {}).get(k) or "") for k in ("notes", "customer_name", "mode_of_transport")).lower()
    text += " " + " ".join(str(i.get("name") or "") for i in (order.raw_json or {}).get("line_items", []))
    if any(x in text for x in ("frozen", "-18", "freezer")):
        return "frozen"
    if any(x in text for x in ("chilled", "cold", "reefer", "dairy")):
        return "chilled"
    return "ambient"


@router.get("/planning/orders", dependencies=[Depends(OPS)])
def planning_orders():
    orders = sorted(live_sales_order_cache.get_current_orders(), key=lambda o: (o.expected_shipment_date or datetime.max.date(), o.salesorder_number or ""))
    return [{"id": o.id, "salesorder_number": o.salesorder_number, "customer_name": o.customer_name, "weight_kg": _weight(o), "cold_chain_category": _cold_chain(o), "assignment_status": o.assignment_status, "vehicle_id": o.vehicle_id, "driver_id": o.driver_id, "manifest_id": o.manifest_id} for o in orders]


class ManifestBody(BaseModel):
    vehicle_id: str
    salesorder_ids: list[str] = Field(min_length=1)


@router.post("/manifests/confirm", dependencies=[Depends(OPS)])
async def confirm_manifest(body: ManifestBody, current_user: CurrentUser = Depends(OPS), db: Session = Depends(get_db)):
    orders = [live_sales_order_cache.find_cached(i) or live_sales_order_cache.ensure_zoho_data(i) for i in body.salesorder_ids]
    if any(o is None for o in orders):
        raise HTTPException(404, "One or more sales orders were not found.")
    orders = [o for o in orders if o is not None]
    if any(o.vehicle_id != body.vehicle_id or o.assignment_status != "assigned" for o in orders):
        raise HTTPException(409, "All selected orders must be assigned to this vehicle.")
    total = sum(_weight(o) for o in orders)
    profile = db.execute(select(Vehicle).where(Vehicle.plate_no == body.vehicle_id)).scalar_one_or_none()
    if profile and profile.rated_capacity_kg is not None and total > float(profile.rated_capacity_kg):
        raise HTTPException(409, f"Manifest exceeds capacity: {total:.1f} kg / {profile.rated_capacity_kg:.1f} kg.")
    manifest = LoadManifest(vehicle_id=body.vehicle_id, status="confirmed", cargo_type="Mixed", total_weight_kg=total, confirmed_at=datetime.now(timezone.utc), confirmed_by=current_user.id)
    db.add(manifest); db.flush()
    for o in orders:
        db.add(ManifestItem(manifest_id=str(manifest.id), salesorder_id=o.id, item_description=o.customer_name, weight_kg=_weight(o), cold_chain_category=_cold_chain(o), quantity=1, cargo_category=_cold_chain(o)))
        o.manifest_id = str(manifest.id); o.assignment_status = "manifested"
        live_sales_order_cache.set_assignment(o.id, manifest_id=manifest.id, assignment_status="manifested")
        sync_history_row(db, o)
    db.commit(); db.refresh(manifest)
    invalidate_fleet_cache()
    _invalidate_assignment_options_cache()
    recipient = (staff_directory_cache.get_by_id(profile.driver_id) if profile and profile.driver_id else None) or staff_directory_cache.first_active()
    if recipient:
        await send_message(SendMessageBody(audience="driver", recipient_id=recipient["id"], channels=["email", "sms", "whatsapp"], subject="Load ready", body=f"Load manifest {manifest.id} is ready for {body.vehicle_id}.", trigger_event="manifest_confirmed", related_so_number=orders[0].salesorder_number))
    return {"manifest": row_to_dict(manifest), "items": [row_to_dict(i) for i in db.execute(select(ManifestItem).where(ManifestItem.manifest_id == str(manifest.id))).scalars().all()]}


@router.get("/warehouse/checklists/{manifest_id}", dependencies=[Depends(OPS)])
def get_checklist(manifest_id: int, db: Session = Depends(get_db)):
    manifest = db.get(LoadManifest, manifest_id)
    if not manifest: raise HTTPException(404, "Manifest not found")
    checklist = db.execute(select(WarehouseLoadingChecklist).where(WarehouseLoadingChecklist.manifest_id == manifest_id)).scalar_one_or_none()
    if checklist is None:
        count = db.execute(select(func.count(ManifestItem.id)).where(ManifestItem.manifest_id == str(manifest_id))).scalar_one()
        checklist = WarehouseLoadingChecklist(manifest_id=manifest_id, cargo_count_expected=int(count))
        db.add(checklist); db.commit(); db.refresh(checklist)
    return {"manifest": row_to_dict(manifest), "checklist": row_to_dict(checklist)}


class ChecklistBody(BaseModel):
    seal_number: str
    cargo_count_actual: int
    departure_temp_c: float
    departure_temp_zone_count: int
    driver_acknowledged: bool


@router.post("/warehouse/checklists/{manifest_id}/complete", dependencies=[Depends(OPS)])
async def complete_checklist(manifest_id: int, body: ChecklistBody, current_user: CurrentUser = Depends(OPS), db: Session = Depends(get_db)):
    manifest = db.get(LoadManifest, manifest_id)
    checklist = db.execute(select(WarehouseLoadingChecklist).where(WarehouseLoadingChecklist.manifest_id == manifest_id)).scalar_one_or_none()
    if not manifest or not checklist: raise HTTPException(404, "Manifest checklist not found")
    if body.cargo_count_actual != checklist.cargo_count_expected: raise HTTPException(409, "Cargo count mismatch; checklist cannot be completed.")
    if not body.driver_acknowledged: raise HTTPException(409, "Driver acknowledgement is required.")
    checklist.seal_number=body.seal_number; checklist.cargo_count_actual=body.cargo_count_actual; checklist.cargo_count_verified=True; checklist.departure_temp_c=body.departure_temp_c; checklist.departure_temp_zone_count=body.departure_temp_zone_count; checklist.driver_acknowledged=True; checklist.checklist_completed=True; checklist.completed_at=datetime.now(timezone.utc); checklist.completed_by=current_user.id
    manifest.status="departed"; db.commit(); db.refresh(checklist); db.refresh(manifest)
    invalidate_fleet_cache()
    _invalidate_assignment_options_cache()
    recipient = staff_directory_cache.first_active()
    if recipient:
        await send_message(SendMessageBody(audience="internal", recipient_id=recipient["id"], channels=["email"], subject="Truck departed", body=f"Truck {manifest.vehicle_id} departed on manifest {manifest.id}.", trigger_event="manifest_departed", related_so_number=None))
    return {"checklist": row_to_dict(checklist), "manifest": row_to_dict(manifest)}


@router.get("/dispatch-dashboard", dependencies=[Depends(get_current_user)])
def dispatch_dashboard(date: str | None = None, refresh: bool = False, db: Session = Depends(get_db)):
    """Live Dispatch Manifest. Current dates use assigned in-memory state + Zoho;
    only the small manifest lookup is read from Neon for departed status."""
    target = date_type.fromisoformat(date) if date else datetime.now(PHT).date()
    key = target.isoformat()
    egress_before = db_egress_stats["query_count"]
    try:
        started = time.monotonic()
        zoho_client.begin_request_metrics()
        if refresh:
            live_sales_order_cache.invalidate_windows()
            live_sales_order_cache.invalidate_assigned_zoho()
        records, zoho_stats = _zoho_date_records(target, refresh)
        list_status_counts = {}
        for record in records.values():
            status_name = live_sales_order_cache._normalized_order_status(record) or "unknown"
            list_status_counts[status_name] = list_status_counts.get(status_name, 0) + 1
        records = {oid: record for oid, record in records.items() if live_sales_order_cache._normalized_order_status(record) not in {"void", "draft"}}
        # Past dates use history only for the assignment join. Zoho remains the
        # authoritative order list and status source for every selected date.
        history_by_id = {}
        if target < datetime.now(PHT).date():
            history_by_id = {str(row.id): row for row in db.execute(select(SalesOrderHistory)).scalars().all()}
        detail_jobs = {}
        detail_cache_hits = 0
        for oid, record in records.items():
            modified = str(record.get("last_modified_time") or "")
            cached_detail = _zoho_detail_cache.get(oid)
            if cached_detail is None or cached_detail[0] != modified:
                detail_jobs[oid] = (record, modified)
            else:
                detail_cache_hits += 1
        def hydrate(item):
            oid, (record, modified) = item
            epoch = zoho_acquisition.generation()
            try:
                detail = zoho_client.fetch_sales_order_detail(oid)
                merged = {**record, **(detail.get("salesorder") or detail)}
                with zoho_acquisition.publication(epoch) as current:
                    if current:
                        _zoho_detail_cache[oid] = (modified, merged)
                return oid, merged
            except Exception:
                return oid, _zoho_detail_cache.get(oid, (modified, record))[1]
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(contextvars.copy_context().run, hydrate, item) for item in detail_jobs.items()]
            for future in as_completed(futures):
                oid, merged = future.result()
                records[oid] = merged
        rows = []
        for oid, record in records.items():
            if live_sales_order_cache._normalized_order_status(record) in {"void", "draft"}: continue
            detail_record = _zoho_detail_cache.get(oid, ("", record))[1]
            row = live_sales_order_cache._build_transient(detail_record)
            if oid in history_by_id:
                history = history_by_id[oid]
                for field in ("vehicle_id", "driver_id", "route_id", "manifest_id", "assigned_at", "assigned_by", "assignment_status", "completed_at", "helper_ids"):
                    setattr(row, field, getattr(history, field, None))
            row = live_sales_order_cache._apply_assignment(row)
            rows.append(row)
        had_fetch_failures = False
        manifest_ids = {int(o.manifest_id) for o in rows if o.manifest_id is not None}
        manifests = {str(m.id): m for m in (db.execute(select(LoadManifest).where(LoadManifest.id.in_(manifest_ids))).scalars().all() if manifest_ids else [])}
        vehicles = {str(v.get("plate_no")): v for v in fleet_static_cache.vehicles()}
        exceptions = []
        moved_without_assignment = 0
        if target >= datetime.now(PHT).date() and locals().get("had_fetch_failures", False):
            exceptions.append({"so_number": "—", "code": "zoho_fetch_failed", "reason": "One or more assigned SOs could not be fetched from Zoho"})
        order_rows = []
        for o in rows:
            raw = getattr(o, "raw_json", {}) or {}
            shipment = str(getattr(o, "shipment_status", None) or raw.get("shipment_status") or "").lower()
            order_status = str(getattr(o, "order_status", None) or raw.get("order_status") or "").lower()
            manifest = manifests.get(str(o.manifest_id)) if o.manifest_id else None
            if shipment == "fulfilled" or order_status == "closed": status = "Delivered"
            elif shipment in {"shipped", "partially_shipped"} or (manifest and manifest.status == "departed"): status = "In Transit"
            else: status = "Pending"
            profile = vehicles.get(str(o.vehicle_id)) if o.vehicle_id else None
            driver = staff_directory_cache.get_by_id(o.driver_id, retry_on_miss=False) if getattr(o, "driver_id", None) else None
            helper_ids = getattr(o, "helper_ids", []) or []
            helper_names = [staff_directory_cache.get_by_id(item, retry_on_miss=False).get("name") for item in helper_ids if staff_directory_cache.get_by_id(item, retry_on_miss=False)]
            helper = " / ".join(helper_names) if helper_names else (driver.get("name") if driver else "NO STAFF")
            if profile and not profile.get("is_third_party") and not driver: exceptions.append({"so_number": o.salesorder_number or o.id, "code": "inhouse_no_driver", "reason": "In-house truck has no driver"})
            if order_status in {"void", "draft"}: exceptions.append({"so_number": o.salesorder_number or o.id, "code": "zoho_void_or_draft", "reason": f"Zoho order status is {order_status}"})
            if not o.vehicle_id and (shipment in {"shipped", "partially_shipped", "fulfilled"} or order_status == "closed"):
                moved_without_assignment += 1
            address = raw.get("shipping_address") or getattr(o, "shipping_address", None) or {}
            if isinstance(address, list): address = address[0] if address else {}
            city = _city((address or {}).get("city"))
            if order_status not in {"confirmed", "acknowledged", "closed", "shipped", "partially_shipped", "fulfilled"}:
                exceptions.append({"so_number": o.salesorder_number or o.id, "code": "not_acknowledged", "reason": "No longer acknowledged/confirmed in Zoho"})
            order_rows.append({"so_number": o.salesorder_number or o.id, "salesorder_id": o.id, "customer": o.customer_name or "—", "city": city, "truck_plate": o.vehicle_id or "Unassigned", "driver_helper": helper, "salesperson": getattr(o, "salesperson_name", None) or raw.get("salesperson_name") or "Unassigned", "status": status, "zoho_order_status": order_status or "—", "zoho_shipped_status": shipment or "—"})
        def group(field):
            grouped = {}
            for item in order_rows:
                name = item[field]
                g = grouped.setdefault(name, {"name": name, "sos": 0, "pending": 0, "in_transit": 0, "delivered": 0})
                g["sos"] += 1; g[item["status"].lower().replace(" ", "_")] += 1
            return sorted(grouped.values(), key=lambda x: (-x["sos"], x["name"]))
        counts = {"sales_orders_today": len(order_rows), "trucks_deployed": len({x["truck_plate"] for x in order_rows if x["truck_plate"] != "Unassigned"}), "unassigned": sum(x["truck_plate"] == "Unassigned" for x in order_rows), "pending": sum(x["status"] == "Pending" for x in order_rows), "in_transit": sum(x["status"] == "In Transit" for x in order_rows), "delivered": sum(x["status"] == "Delivered" for x in order_rows)}
        status_counts = list_status_counts
        total = counts["sales_orders_today"] or 1
        payload = {"date": key, "synced_at": datetime.now(PHT).isoformat(), "source": "Confirmed SO — IntelliFleet", "cross_checked": "Zoho Inventory", "sync_error": None, "moved_without_assignment": moved_without_assignment, "kpis": {**counts, "exceptions_count": len(exceptions), **{f"{k}_pct": round(counts[k] * 100 / total, 1) for k in ("pending", "in_transit", "delivered")}}, "exceptions": exceptions, "trucks": group("truck_plate"), "salespeople": group("salesperson"), "pending_orders": [{"so_number": x["so_number"], "customer": x["customer"], "truck": x["truck_plate"]} for x in order_rows if x["status"] == "Pending"], "orders": order_rows}
        payload["neon_queries"] = db_egress_stats["query_count"] - egress_before
        payload["zoho_api_calls"] = zoho_client.api_call_count()
        payload["zoho_diagnostics"] = {**zoho_stats, "rows_after_void_draft": len(records), "detail_hydrations": len(detail_jobs), "detail_cache_hits": detail_cache_hits, "shipment_date_count": len(records), "order_date_count": sum(live_sales_order_cache._as_date(r.get("date") or r.get("order_date")) == target for r in records.values()), "status_counts": status_counts, "neon_queries": payload["neon_queries"], "elapsed_ms": int((time.monotonic() - started) * 1000)}
        logger.info("[DISPATCH_DASHBOARD] date=%s neon_queries=%s orders=%s", key, payload["neon_queries"], len(order_rows))
        _dashboard_last_good[key] = payload
        return payload
    except Exception as exc:
        previous = _dashboard_last_good.get(key)
        if previous:
            return {**previous, "sync_error": str(exc)}
        raise HTTPException(502, f"Dispatch dashboard sync failed: {exc}") from exc
