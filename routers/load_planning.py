from __future__ import annotations

import base64
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth.dependencies import require_role
from database import get_db
from models.inventory import SalesOrderCache
from models.sales_order_history import SalesOrderHistory
from models.sales_order_lines import SalesOrderLine
from services import staff_directory_cache, live_sales_order_cache
from services import zoho_acquisition
from services.serialize import row_to_dict
from services.zoho_client import (
    ZohoError,
    acknowledge_sales_order,
    fetch_sales_order_detail,
    fetch_sales_orders_by_customview,
    remove_acknowledge_sales_order,
)
from services.inventory_exports import flatten_order, make_excel, make_pdf
from services.openai_client import generate_sales_order_email_draft
from services.item_weight import calculate_line_weight_kg
from services.delivery_status import is_delivered, sales_order_delivery_status
from services.warehouse_stock import stock_for_orders
from services.sales_order_location import address_object, find_city, shipping_city
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/load-planning", tags=["load-planning"], dependencies=[Depends(require_role("admin", "dispatcher", "warehouse"))])
logger = logging.getLogger("load_planning")
PHT = ZoneInfo("Asia/Manila")
_refresh_lock = Lock()
_refresh_state = {"running": False, "synced_count": 0, "error": None, "finished_at": None}
# Zoho's own saved "Acknowledged" Sales Orders custom view, so filtering here matches
# exactly what a dispatcher sees filtering inside Zoho's UI instead of reconstructing
# Zoho's internal sub-status logic locally.
ACKNOWLEDGED_CUSTOMVIEW_ID = os.environ.get("ZOHO_ACKNOWLEDGED_CUSTOMVIEW_ID", "4489499000002275225")


def _pick(record: dict, *keys: str):
    """Return the first non-null Zoho field variant."""
    for key in keys:
        value = record.get(key)
        if value is not None:
            return value
    return None


def _fetch_acknowledged_ids_from_zoho() -> set[str]:
    ids: set[str] = set()
    page = 1
    while True:
        payload = fetch_sales_orders_by_customview(ACKNOWLEDGED_CUSTOMVIEW_ID, page=page, per_page=200)
        records = payload.get("salesorders") or []
        for record in records:
            record_id = str(_pick(record, "salesorder_id", "sales_order_id", "id") or "")
            if record_id:
                ids.add(record_id)
        context = payload.get("page_context") or {}
        has_more = context.get("has_more_page")
        if isinstance(has_more, str):
            has_more = has_more.strip().lower() == "true"
        if not records or not has_more:
            break
        page += 1
    return ids


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, "Dates must use YYYY-MM-DD format.") from exc


_normalized_order_status = live_sales_order_cache._normalized_order_status


def _summary(row, db: Session | None = None) -> dict:
    result = row_to_dict(row)
    result["id"] = row.id
    if result.get("total") is not None:
        result["total"] = float(result["total"])
    if row.driver_id:
        driver = staff_directory_cache.get_by_id(row.driver_id)
        result["driver_name"] = driver["name"] if driver else None
    is_persisted = isinstance(row, SalesOrderHistory)  # slim sales_orders row - no raw_json (Step 6)
    address = _address_object(row.shipping_address or (None if is_persisted else (row.raw_json or {}).get("shipping_address")))
    result["shipping_city"] = _shipping_city(row)
    result["shipping_address"] = address
    raw_notes = {} if is_persisted else (row.raw_json or {})
    notes = raw_notes.get("notes") or raw_notes.get("note") or raw_notes.get("customer_notes")
    result["notes"] = str(notes).strip() if notes is not None and str(notes).strip() else None
    if is_persisted:
        result["mets_qty_available_for_sale"] = float(row.mets_qty_available_for_sale) if row.mets_qty_available_for_sale is not None else None
        result["glacier_qty_available_for_sale"] = float(row.glacier_qty_available_for_sale) if row.glacier_qty_available_for_sale is not None else None
    else:
        result["mets_qty_available_for_sale"] = getattr(row, "_mets_qty_available_for_sale", None)
        result["glacier_qty_available_for_sale"] = getattr(row, "_glacier_qty_available_for_sale", None)

    def number(item, *keys):
        for key in keys:
            value = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
            if value is not None:
                try: return float(value)
                except (TypeError, ValueError): pass
        return 0.0

    products = []
    total_packs = 0.0
    total_cases = 0.0
    total_units = 0.0

    if is_persisted:
        # sales_orders is slim - line items live in sales_order_lines, weight precomputed
        # at write time (services/sales_order_history_sync.py), never recalculated on read.
        lines = db.execute(select(SalesOrderLine).where(SalesOrderLine.sales_order_id == row.id)).scalars().all() if db is not None else []
        for line in lines:
            quantity = float(line.quantity or 0)
            unit = str(line.unit or "").strip()
            normalized = unit.casefold()
            if "pack" in normalized: total_packs += quantity
            elif "case" in normalized or "carton" in normalized: total_cases += quantity
            else: total_units += quantity
            products.append({"line_item_id": None, "item_id": line.item_id, "name": line.name, "sku": line.sku, "quantity": quantity, "unit": unit or None, "total_weight_kg": line.weight_kg, "packaging_type": "pack" if "pack" in normalized else "case" if "case" in normalized or "carton" in normalized else None, "pack_quantity": quantity if "pack" in normalized else 0, "case_quantity": quantity if "case" in normalized or "carton" in normalized else 0, "quantity_packed": 0.0, "quantity_shipped": float(line.quantity_shipped or 0)})
        total_item_quantity = sum(float(line.quantity or 0) for line in lines)
    else:
        items = (row.raw_json or {}).get("line_items") or []

        def item_weight_kg(item: dict, order_number: str) -> float | None:
            quantity = item.get("quantity")
            unit = item.get("unit") or item.get("unit_name") or item.get("usage_unit")
            nested_item = item.get("item") if isinstance(item.get("item"), dict) else {}
            item_id = item.get("item_id") or item.get("itemid") or nested_item.get("item_id") or nested_item.get("id")
            return calculate_line_weight_kg(quantity, unit, item_id, item=item, context=f"SO={order_number} SKU={item.get('sku')}")

        for item in items:
            quantity = number(item, "quantity")
            unit = str(item.get("unit") or item.get("unit_name") or item.get("usage_unit") or "").strip()
            normalized = unit.casefold()
            if "pack" in normalized: total_packs += quantity
            elif "case" in normalized or "carton" in normalized: total_cases += quantity
            else: total_units += quantity
            line_total_weight_kg = item_weight_kg(item, row.salesorder_number or row.id)
            products.append({"line_item_id": item.get("line_item_id"), "item_id": item.get("item_id") or item.get("itemid"), "name": item.get("name") or item.get("item_description") or item.get("description"), "sku": item.get("sku") or item.get("item_order") or item.get("item_id"), "quantity": quantity, "unit": unit or None, "total_weight_kg": line_total_weight_kg, "packaging_type": "pack" if "pack" in normalized else "case" if "case" in normalized or "carton" in normalized else None, "pack_quantity": quantity if "pack" in normalized else 0, "case_quantity": quantity if "case" in normalized or "carton" in normalized else 0, "quantity_packed": number(item, "quantity_packed"), "quantity_shipped": number(item, "quantity_shipped")})
        total_item_quantity = sum(number(item, "quantity") for item in items)

    result["product_count"] = len([p for p in products if p["name"]])
    result["products"] = products
    result["pack_count"] = total_packs
    result["case_count"] = total_cases
    result["unit_count"] = total_units
    result["total_item_quantity"] = total_item_quantity
    return result


@router.get("/inventory/sales-orders")
def list_sales_orders(
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    status: str | None = Query(None),
    search: str | None = Query(None),
    assignment: str | None = Query(None, pattern="^(assigned|unassigned)$"),
    cities: str | None = Query(None, description="Comma-separated destination cities"),
    vehicle: str | None = Query(None),
    customer: str | None = Query(None),
    delivery_status: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
):
    rows = _filtered_rows(db, date_from, date_to, status, search, assignment, cities, vehicle, customer, delivery_status)
    stock = stock_for_orders(rows) if rows and not all(isinstance(row, SalesOrderHistory) for row in rows) else {}
    for row in rows:
        setattr(row, "_mets_qty_available_for_sale", stock.get(f"{row.id}:mets"))
        setattr(row, "_glacier_qty_available_for_sale", stock.get(f"{row.id}:glacier"))
    start_index = (page - 1) * per_page
    page_rows = rows[start_index : start_index + per_page]
    return {
        "items": [_summary(row, db) for row in page_rows],
        "page": page,
        "per_page": per_page,
        "total": len(rows),
        "has_more": start_index + per_page < len(rows),
    }


_address_object = address_object


_find_city = find_city


_shipping_city = shipping_city


@router.get("/inventory/sales-orders/cities")
def list_sales_order_cities(date_from: str | None = Query(None), date_to: str | None = Query(None)):
    # City options must not disappear just because the currently selected date
    # window has no orders. The date range is applied when filtering the cards.
    # No unbounded "all SOs ever" query any more - Zoho is the source, live-cached
    # per window (services/live_sales_order_cache.py). Defaults to a 2-week span when no
    # range is given. hydrate=False: city only needs shipping_address, not full line_items -
    # skips the expensive per-order Zoho detail fetch.
    start = _date(date_from) or (datetime.now(PHT).date() - timedelta(days=7))
    end = _date(date_to) or (datetime.now(PHT).date() + timedelta(days=7))
    rows = live_sales_order_cache.get_window(start, end, hydrate=False)
    return {"cities": sorted({_shipping_city(row) for row in rows if _shipping_city(row)}, key=str.casefold)}


def _filtered_rows(db: Session, date_from: str | None, date_to: str | None, status: str | None, search: str | None, assignment: str | None = None, cities: str | None = None, vehicle: str | None = None, customer: str | None = None, delivery_status: str | None = None):
    start = _date(date_from) or datetime.now(PHT).date(); end = _date(date_to) or start
    if end < start: raise HTTPException(400, "date_to must be on or after date_from.")
    today = datetime.now(PHT).date()
    if assignment == "assigned" and end < today:
        # Earlier calendar dates for Confirmed SO: Neon (sales_order_history) only - no
        # Zoho call, no live-cache, no sales_orders_cache read at all.
        rows = db.execute(select(SalesOrderHistory).where(SalesOrderHistory.expected_shipment_date >= start, SalesOrderHistory.expected_shipment_date <= end).order_by(SalesOrderHistory.expected_shipment_date.desc(), SalesOrderHistory.salesorder_number.desc())).scalars().all()
    elif assignment == "assigned":
        # Current/future range for Confirmed SO: the in-memory assigned snapshot (Zoho data,
        # kept warm live; assignment state kept warm from sales_order_history at startup and
        # updated on every mutation). Zero Neon reads.
        rows = [r for r in live_sales_order_cache.get_assigned_snapshot() if r.expected_shipment_date is not None and start <= r.expected_shipment_date <= end]
    else:
        # Unassigned / no assignment filter, any date: live Zoho window cache (5-min TTL).
        # No sales_orders_cache read/write at all any more.
        rows = live_sales_order_cache.get_window(start, end)
    needle = (search or "").lower(); wanted = (status or "").lower()
    acknowledged_ids = _fetch_acknowledged_ids_from_zoho() if wanted.replace("_", " ") == "acknowledged" else None
    def matches_status(row: SalesOrderCache) -> bool:
        if not wanted or wanted == "all":
            return True
        if acknowledged_ids is not None:
            return str(row.id) in acknowledged_ids
        actual = _normalized_order_status(getattr(row, "raw_json", None) or {}) or str(row.order_status or "")
        return actual.strip().lower().replace("_", " ") == wanted.replace("_", " ")
    wanted_cities = {value.strip().casefold() for value in (cities or "").split(",") if value.strip()}
    # Confirmed SO history includes active and soft-completed assignments.
    assignment_match = lambda row: not assignment or ((row.assignment_status or "unassigned") in {assignment, "completed"} if assignment == "assigned" else (row.assignment_status or "unassigned") == assignment)
    vehicle_needle = (vehicle or "").strip().casefold()
    customer_needle = (customer or "").strip().casefold()
    delivery_needle = (delivery_status or "").strip().casefold()
    def row_delivery_status(row) -> str:
        if isinstance(row, SalesOrderHistory):
            return str(row.delivery_status or "Unknown")
        raw = row.raw_json or {}
        if is_delivered(raw):
            return "Delivered"
        return str(raw.get("shipment_status") or raw.get("shipping_status") or "Pending")
    return [row for row in rows if assignment_match(row) and not (assignment == "assigned" and is_delivered(getattr(row, "raw_json", None) or {}) and row.assignment_status != "completed") and matches_status(row) and (not wanted_cities or (_shipping_city(row) or "").casefold() in wanted_cities) and (not vehicle_needle or vehicle_needle in str(row.vehicle_id or "").casefold()) and (not customer_needle or customer_needle in str(row.customer_name or "").casefold()) and (not delivery_needle or delivery_needle in row_delivery_status(row).casefold()) and (not needle or needle in " ".join(str(x or "") for x in (row.salesorder_number, row.customer_name, row.reference_number, row.vehicle_id, _shipping_city(row) or "")).lower())]


def _hydrate_export_rows(db: Session, rows: list[SalesOrderCache]) -> list[SalesOrderCache]:
    """Ensure exports use full Zoho detail payloads, not compact list-cache rows. Mutates
    each row's raw_json in place - since these are the same live-cache-held instances (or
    sales_order_history rows for past dates), no separate persistence step is needed."""
    for row in rows:
        raw = row.raw_json or {}
        line_items = raw.get("line_items") if isinstance(raw, dict) else None
        if isinstance(line_items, list) and any(isinstance(item, dict) and item.get("name") for item in line_items):
            continue
        try:
            detail = fetch_sales_order_detail(str(row.id))
            record = detail.get("salesorder") or detail
            row.raw_json = {**raw, **{k: v for k, v in record.items() if v not in (None, "", [], {})}}
        except ZohoError:
            continue
    return rows


def _download_stamp() -> str:
    return datetime.now(PHT).strftime("%Y-%m-%d_%H-%M-%S")


class EmailFilterContext(BaseModel):
    date_from: str | None = None
    date_to: str | None = None
    status: str | None = None
    search: str | None = None
    order_ids: list[str] = Field(default_factory=list)
    assignment: str | None = None


class EmailDraftRequest(EmailFilterContext):
    pass


class EmailSendRequest(EmailFilterContext):
    to: str
    subject: str
    htmlBody: str


def _email_rows(db: Session, context: EmailFilterContext) -> list[SalesOrderCache]:
    rows = _filtered_rows(db, context.date_from, context.date_to, context.status, context.search, context.assignment)
    if context.order_ids:
        wanted = {str(value) for value in context.order_ids}
        rows = [row for row in rows if str(row.id) in wanted]
    return _hydrate_export_rows(db, rows)


def _email_context(rows: list[SalesOrderCache], context: EmailFilterContext) -> dict:
    return {
        "dateFrom": context.date_from,
        "dateTo": context.date_to,
        "status": context.status or "All",
        "search": context.search or "",
        "orderCount": len(rows),
        "orders": [
            {
                "salesOrderNumber": row.salesorder_number,
                "customerName": row.customer_name,
                "expectedShipmentDate": row.expected_shipment_date.isoformat() if row.expected_shipment_date else None,
                "status": row.order_status,
                "total": float(row.total) if row.total is not None else None,
            }
            for row in rows[:50]
        ],
    }


@router.post("/email/draft")
async def email_draft(body: EmailDraftRequest, db: Session = Depends(get_db)):
    rows = _email_rows(db, body)
    return await generate_sales_order_email_draft(_email_context(rows, body))


@router.post("/email/send")
async def email_send(body: EmailSendRequest, db: Session = Depends(get_db)):
    if "@" not in body.to or any(not part.strip() for part in body.to.split("@", 1)):
        raise HTTPException(422, "Enter a valid recipient email address.")
    webhook_secret = os.environ.get("INTELLIFLEET_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        raise HTTPException(501, "INTELLIFLEET_WEBHOOK_SECRET is not configured.")
    rows = _email_rows(db, body)
    export_rows = [line for order in rows for line in flatten_order(order)]
    start = body.date_from or datetime.now(PHT).date().isoformat()
    end = body.date_to or start
    stamp = _download_stamp()
    caption = f"{start} to {end} - {body.status or 'All'} statuses - Downloaded {stamp.replace('_', ' ')} PHT"
    pdf = make_pdf(export_rows, caption).getvalue()
    excel = make_excel(export_rows, caption).getvalue()
    payload = {
        "to": body.to.strip(),
        "subject": body.subject.strip(),
        "htmlBody": body.htmlBody,
        "orderIds": [str(row.id) for row in rows],
        "filterContext": _email_context(rows, body),
        "attachments": [
            {"filename": "SalesOrders.pdf", "mimeType": "application/pdf", "content": base64.b64encode(pdf).decode("ascii")},
            {"filename": "SalesOrders.xlsx", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "content": base64.b64encode(excel).decode("ascii")},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                "https://rareglobalfood.app.n8n.cloud/webhook/intellifleet-send-sales-order-email",
                headers={"Authorization": webhook_secret},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, "The email delivery service could not be reached.") from exc
    try:
        result = response.json()
    except ValueError:
        result = {"success": response.is_success, "message": response.text}
    if not response.is_success:
        raise HTTPException(502, result if isinstance(result, str) else result)
    return result




@router.get("/inventory/export/excel")
def export_excel(date_from: str | None = Query(None), date_to: str | None = Query(None), status: str | None = Query(None), search: str | None = Query(None), assignment: str | None = Query(None), db: Session = Depends(get_db)):
    rows = _hydrate_export_rows(db, _filtered_rows(db, date_from, date_to, status, search, assignment)); start = date_from or datetime.now(PHT).date().isoformat(); end = date_to or start; downloaded = _download_stamp(); caption = f"{start} to {end} - {status or 'All'} statuses - Downloaded {downloaded.replace('_', ' ')} PHT"
    return StreamingResponse(make_excel([line for order in rows for line in flatten_order(order)], caption), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="RGF_SalesOrders_{start}_to_{end}_downloaded_{downloaded}.xlsx"'})


@router.get("/inventory/export/pdf")
def export_pdf(date_from: str | None = Query(None), date_to: str | None = Query(None), status: str | None = Query(None), search: str | None = Query(None), assignment: str | None = Query(None), db: Session = Depends(get_db)):
    rows = _hydrate_export_rows(db, _filtered_rows(db, date_from, date_to, status, search, assignment)); start = date_from or datetime.now(PHT).date().isoformat(); end = date_to or start; downloaded = _download_stamp(); caption = f"{start} to {end} - {status or 'All'} statuses - Downloaded {downloaded.replace('_', ' ')} PHT"
    return StreamingResponse(make_pdf([line for order in rows for line in flatten_order(order)], caption), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="RGF_SalesOrders_{start}_to_{end}_downloaded_{downloaded}.pdf"'})


def _run_sales_order_sync(start: date | None, end: date | None) -> None:
    """The "Refresh" button: force-invalidate the live Zoho window cache (and every
    currently-assigned SO's cached Zoho data) so the next read re-pulls fresh from Zoho.
    No Neon writes here at all any more - Zoho is re-fetched live, on demand."""
    try:
        live_sales_order_cache.invalidate_windows()
        live_sales_order_cache.invalidate_assigned_zoho()
        synced = len(live_sales_order_cache.get_window(start or datetime.now(PHT).date(), end or datetime.now(PHT).date()))
        with _refresh_lock:
            _refresh_state.update(running=False, synced_count=synced, error=None, finished_at=datetime.now(timezone.utc).isoformat())
    except ZohoError as exc:
        with _refresh_lock:
            _refresh_state.update(running=False, synced_count=0, error=str(exc), finished_at=datetime.now(timezone.utc).isoformat())


@router.get("/inventory/refresh/status")
def refresh_sales_orders_status():
    with _refresh_lock:
        return dict(_refresh_state)


@router.post("/inventory/refresh")
def refresh_sales_orders(
    background_tasks: BackgroundTasks,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    start = _date(date_from)
    end = _date(date_to)
    if (start is None) != (end is None):
        raise HTTPException(400, "date_from and date_to must be provided together.")
    if start and end and end < start:
        raise HTTPException(400, "date_to must be on or after date_from.")
    with _refresh_lock:
        if _refresh_state["running"]:
            return {"sync_started": False, **_refresh_state}
        _refresh_state.update(running=True, synced_count=0, error=None, finished_at=None)
    background_tasks.add_task(_run_sales_order_sync, start, end)
    return {"sync_started": True, "synced_count": 0, "synced_at": datetime.now(timezone.utc).isoformat()}


@router.post("/inventory/sales-orders/{salesorder_id}/acknowledge")
def acknowledge_sales_order_route(salesorder_id: str):
    cached = live_sales_order_cache.find_cached(salesorder_id) or live_sales_order_cache.ensure_zoho_data(salesorder_id)
    if cached is None:
        raise HTTPException(404, "Sales order was not found.")
    current_status = str(cached.order_status or "").lower().replace("_", " ")
    if current_status in {"acknowledged", "void", "cancelled", "canceled"}:
        raise HTTPException(409, "This sales order is locked and cannot be acknowledged.")
    try:
        result = acknowledge_sales_order(salesorder_id)
        live_sales_order_cache.refresh_zoho_data(salesorder_id)
        live_sales_order_cache.invalidate_windows()
        return {"acknowledged": True, **result}
    except ZohoError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/inventory/sales-orders/{salesorder_id}/remove-acknowledge")
@zoho_acquisition.operation("remove-acknowledgement")
def remove_acknowledge_sales_order_route(salesorder_id: str):
    cached = live_sales_order_cache.find_cached(salesorder_id) or live_sales_order_cache.ensure_zoho_data(salesorder_id)
    if cached is None:
        raise HTTPException(404, "Sales order was not found.")
    current_status = str(cached.order_status or "").lower().replace("_", " ")
    if current_status != "acknowledged":
        raise HTTPException(409, "This sales order is not acknowledged.")
    try:
        remove_acknowledge_sales_order(salesorder_id)
        epoch = zoho_acquisition.generation()
        detail = fetch_sales_order_detail(salesorder_id)
        live_sales_order_cache.publish_zoho_data(salesorder_id, detail, epoch)
        live_sales_order_cache.invalidate_windows()
        return {"removed_acknowledge": True, "status": "confirmed", **detail}
    except ZohoError as exc:
        raise HTTPException(502, f"Zoho status changed could not be synchronized locally: {exc}") from exc


@router.post("/inventory/sales-orders/acknowledge-filtered")
def acknowledge_filtered_sales_orders(
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    status: str | None = Query(None),
    search: str | None = Query(None),
    db: Session = Depends(get_db),
):
    rows = _filtered_rows(db, date_from, date_to, status, search)
    eligible = [row for row in rows if str(row.order_status or "").lower().replace("_", " ") not in {"acknowledged", "void", "cancelled", "canceled"}]
    acknowledged = 0
    failed = 0
    for row in eligible:
        try:
            acknowledge_sales_order(str(row.id))
            live_sales_order_cache.refresh_zoho_data(str(row.id))
            acknowledged += 1
        except ZohoError:
            failed += 1
    if acknowledged:
        live_sales_order_cache.invalidate_windows()
    return {"filtered_count": len(rows), "eligible_count": len(eligible), "acknowledged_count": acknowledged, "failed_count": failed}


@router.get("/inventory/sales-orders/{salesorder_id}")
@zoho_acquisition.operation("inventory-drawer")
def get_sales_order(salesorder_id: str):
    cached = live_sales_order_cache.find_cached(salesorder_id)
    try:
        # List responses are intentionally compact. Fetch the detail payload on every
        # open so the drawer reflects current addresses, line items, totals and fields.
        epoch = zoho_acquisition.generation()
        detail = fetch_sales_order_detail(salesorder_id)
        live_sales_order_cache.publish_zoho_data(salesorder_id, detail, epoch)
        record = detail.get("salesorder") or detail
        return {"cached": False, "delivery_status": sales_order_delivery_status(record), **detail}
    except ZohoError as exc:
        if cached and cached.raw_json:
            return {"cached": True, "synced_at": cached.synced_at, **cached.raw_json}
        raise HTTPException(502, str(exc)) from exc
