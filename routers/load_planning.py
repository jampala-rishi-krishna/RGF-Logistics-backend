from __future__ import annotations

import contextvars
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, require_role
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
from services.zoho_so_lock import lock_salesorder, lock_status_from_record
from services.inventory_exports import confirmed_item_ids, flatten_confirmed_order, flatten_order, make_excel, make_pdf
from services.openai_client import generate_sales_order_email_draft
from services.item_weight import calculate_line_weight_kg
from services.delivery_status import is_delivered, sales_order_delivery_status
from services import item_detail_cache
from services.warehouse_stock import cached_item_stock, fetch_item_stock, stock_for_orders_cached
from services.sales_order_location import address_object, find_city, shipping_city
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from services import gmail_sender

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


# The Acknowledged custom view used to be re-paged from Zoho on every list request. Cache it
# briefly; Refresh drops it. Local acknowledge / remove-acknowledge results are NOT written into
# this cache (a re-fetch would replace them wholesale with whatever Zoho's view returns, which can
# lag a just-made change); they live in _ack_overrides and are laid over the Zoho set on every read.
ACK_IDS_TTL_SECONDS = 120
# How long a local acknowledge/remove result keeps winning over Zoho's view if the view never
# catches up (it normally does within seconds, at which point the override is dropped).
ACK_OVERRIDE_TTL_SECONDS = 600
_ack_lock = Lock()
_ack_cache: dict = {"ids": None, "at": 0.0}
_ack_overrides: dict[str, tuple[bool, float]] = {}


def _with_overrides(zoho_ids: set[str]) -> tuple[set[str], dict[str, bool]]:
    """Zoho's Acknowledged-view ids with locally confirmed changes applied. An override is dropped
    once Zoho's view agrees with it, or when it is older than ACK_OVERRIDE_TTL_SECONDS."""
    now = time.monotonic()
    result = set(zoho_ids)
    live: dict[str, bool] = {}
    with _ack_lock:
        for order_id, (acknowledged, at) in list(_ack_overrides.items()):
            if (order_id in zoho_ids) == acknowledged or now - at > ACK_OVERRIDE_TTL_SECONDS:
                del _ack_overrides[order_id]
            else:
                live[order_id] = acknowledged
                (result.add if acknowledged else result.discard)(order_id)
    return result, live


def _ack_snapshot() -> tuple[set[str], dict[str, bool]]:
    """(Zoho Acknowledged-view ids with local changes applied, the still-live local overrides)."""
    with _ack_lock:
        fresh = _ack_cache["ids"] is not None and time.monotonic() - _ack_cache["at"] < ACK_IDS_TTL_SECONDS
        zoho_ids = set(_ack_cache["ids"]) if fresh else None
    if zoho_ids is None:
        zoho_ids = _fetch_acknowledged_ids_from_zoho()
        with _ack_lock:
            _ack_cache.update(ids=set(zoho_ids), at=time.monotonic())
    return _with_overrides(zoho_ids)


def _acknowledged_ids() -> set[str]:
    return _ack_snapshot()[0]


def _set_acknowledged(order_id: str, acknowledged: bool) -> None:
    with _ack_lock:
        _ack_overrides[str(order_id)] = (acknowledged, time.monotonic())


def _invalidate_ack_cache() -> None:
    with _ack_lock:
        _ack_cache.update(ids=None, at=0.0)


def _explicit_sub_status(row) -> str:
    raw = row.raw_json if isinstance(getattr(row, "raw_json", None), dict) else {}
    return live_sales_order_cache._squash(raw.get("current_sub_status") or raw.get("order_sub_status"))


def row_is_acknowledged(row, view_ids: set[str], overrides: dict[str, bool] | None = None) -> bool:
    """The single decision for "is this order acknowledged". Order of authority:
    1. a recent local acknowledge / remove-acknowledge (beats a Zoho payload that has not caught up),
    2. the order's own sub-status (current_sub_status / order_sub_status == cs_acknowl),
    3. Zoho's Acknowledged custom view - only when the record carries no sub-status at all."""
    order_id = str(row.id)
    if overrides and order_id in overrides:
        return overrides[order_id]
    sub_status = _explicit_sub_status(row)
    if sub_status:
        return sub_status == "csacknowl"
    if order_id in view_ids or str(getattr(row, "order_status", "") or "").lower() == "acknowledged":
        return True
    return live_sales_order_cache.cached_detail_has_acknowledged_sub_status(order_id)


def _is_acknowledged(row) -> bool:
    raw = row.raw_json if isinstance(getattr(row, "raw_json", None), dict) else {}
    order_id = str(getattr(row, "id", "") or "")
    return (
        live_sales_order_cache.record_has_acknowledged_sub_status(raw)
        or str(row.order_status or "").lower() == "acknowledged"
        or bool(order_id and live_sales_order_cache.cached_detail_has_acknowledged_sub_status(order_id))
    )


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, "Dates must use YYYY-MM-DD format.") from exc


_normalized_order_status = live_sales_order_cache._normalized_order_status


def _summary(row, db: Session | None = None, *, allow_fetch: bool = True) -> dict:
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
    result["zoho_lock"] = lock_status_from_record(raw_notes)
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
            # The order-level stock saved at assignment time is a point-in-time snapshot that may
            # be wrong; show Zoho's own per-item "Available for Sale" instead.
            saved_item_stock = ((fetch_item_stock(str(line.item_id)) if allow_fetch else cached_item_stock(str(line.item_id))) if line.item_id else None) or {}
            if "pack" in normalized: total_packs += quantity
            elif "case" in normalized or "carton" in normalized: total_cases += quantity
            else: total_units += quantity
            products.append({"mets_qty_available_for_sale": saved_item_stock.get("mets"), "glacier_qty_available_for_sale": saved_item_stock.get("glacier"), "line_item_id": None, "item_id": line.item_id, "name": line.name, "sku": line.sku, "quantity": quantity, "unit": unit or None, "total_weight_kg": line.weight_kg, "packaging_type": "pack" if "pack" in normalized else "case" if "case" in normalized or "carton" in normalized else None, "pack_quantity": quantity if "pack" in normalized else 0, "case_quantity": quantity if "case" in normalized or "carton" in normalized else 0, "quantity_packed": 0.0, "quantity_shipped": float(line.quantity_shipped or 0)})
        total_item_quantity = sum(float(line.quantity or 0) for line in lines)
    else:
        items = (row.raw_json or {}).get("line_items") or []

        def item_weight_kg(item: dict, order_number: str) -> float | None:
            quantity = item.get("quantity")
            unit = item.get("unit") or item.get("unit_name") or item.get("usage_unit")
            nested_item = item.get("item") if isinstance(item.get("item"), dict) else {}
            item_id = item.get("item_id") or item.get("itemid") or nested_item.get("item_id") or nested_item.get("id")
            return calculate_line_weight_kg(quantity, unit, item_id, item=item, context=f"SO={order_number} SKU={item.get('sku')}", allow_fetch=allow_fetch)

        for item in items:
            quantity = number(item, "quantity")
            unit = str(item.get("unit") or item.get("unit_name") or item.get("usage_unit") or "").strip()
            normalized = unit.casefold()
            if "pack" in normalized: total_packs += quantity
            elif "case" in normalized or "carton" in normalized: total_cases += quantity
            else: total_units += quantity
            line_total_weight_kg = item_weight_kg(item, row.salesorder_number or row.id)
            stock_item_id = item.get("item_id") or item.get("itemid")
            item_stock = ((fetch_item_stock(str(stock_item_id)) if allow_fetch else cached_item_stock(str(stock_item_id))) if stock_item_id else None) or {}
            products.append({"mets_qty_available_for_sale": item_stock.get("mets"), "glacier_qty_available_for_sale": item_stock.get("glacier"), "line_item_id": item.get("line_item_id"), "item_id": item.get("item_id") or item.get("itemid"), "name": item.get("name") or item.get("item_description") or item.get("description"), "sku": item.get("sku") or item.get("item_order") or item.get("item_id"), "quantity": quantity, "unit": unit or None, "total_weight_kg": line_total_weight_kg, "packaging_type": "pack" if "pack" in normalized else "case" if "case" in normalized or "carton" in normalized else None, "pack_quantity": quantity if "pack" in normalized else 0, "case_quantity": quantity if "case" in normalized or "carton" in normalized else 0, "quantity_packed": number(item, "quantity_packed"), "quantity_shipped": number(item, "quantity_shipped")})
        total_item_quantity = sum(number(item, "quantity") for item in items)

    result["product_count"] = len([p for p in products if p["name"]])
    result["products"] = products
    result["pack_count"] = total_packs
    result["case_count"] = total_cases
    result["unit_count"] = total_units
    result["total_item_quantity"] = total_item_quantity
    return result


def _total_weight_kg(rows, db: Session) -> tuple[float, bool]:
    """Total kg across every filtered order (not just the current page), from cached item
    weights only. The bool is False while any line's weight is still unknown."""
    total = 0.0
    complete = True
    for row in rows:
        if isinstance(row, SalesOrderHistory):
            continue
        for item in (row.raw_json or {}).get("line_items") or []:
            if not isinstance(item, dict) or item.get("quantity") in (None, ""):
                continue
            nested = item.get("item") if isinstance(item.get("item"), dict) else {}
            item_id = item.get("item_id") or item.get("itemid") or nested.get("item_id") or nested.get("id")
            weight = calculate_line_weight_kg(item.get("quantity"), item.get("unit") or item.get("unit_name") or item.get("usage_unit"), item_id, item=item, context="total", allow_fetch=False)
            if weight is None:
                complete = False
            else:
                total += weight
    history_ids = [row.id for row in rows if isinstance(row, SalesOrderHistory)]
    if history_ids:
        for weight in db.execute(select(SalesOrderLine.weight_kg).where(SalesOrderLine.sales_order_id.in_(history_ids))).scalars().all():
            if weight is None:
                complete = False
            else:
                total += float(weight)
    return round(total, 3), complete


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
    # Never block the list on per-item Zoho calls: serve cached stock/weights (stale is fine),
    # fill the gaps in the background, and tell the UI to poll while anything is still missing.
    live_rows = [row for row in rows if not isinstance(row, SalesOrderHistory)]
    stock, waiting = stock_for_orders_cached(live_rows) if live_rows else ({}, 0)
    for row in rows:
        setattr(row, "_mets_qty_available_for_sale", stock.get(f"{row.id}:mets"))
        setattr(row, "_glacier_qty_available_for_sale", stock.get(f"{row.id}:glacier"))
    start_index = (page - 1) * per_page
    page_rows = rows[start_index : start_index + per_page]
    saved_page = [row.id for row in page_rows if isinstance(row, SalesOrderHistory)]
    if saved_page:
        # Past-dated orders: queue a background fetch of each line's item so its stock fills in.
        waiting += item_detail_cache.request_refresh(db.execute(select(SalesOrderLine.item_id).where(SalesOrderLine.sales_order_id.in_(saved_page))).scalars().all())
    total_weight, weight_complete = _total_weight_kg(rows, db)
    return {
        "items": [_summary(row, db, allow_fetch=False) for row in page_rows],
        "page": page,
        "per_page": per_page,
        "total": len(rows),
        "has_more": start_index + per_page < len(rows),
        "stock_pending": waiting > 0,
        "total_weight_kg": total_weight,
        "weight_complete": weight_complete and waiting == 0,
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
    wanted_label = wanted.replace("_", " ")
    acknowledged_ids, ack_overrides = _ack_snapshot() if wanted_label in {"acknowledged", "all except acknowledged"} else (None, {})
    def matches_status(row: SalesOrderCache) -> bool:
        if not wanted or wanted == "all":
            return True
        actual_status = _normalized_order_status(getattr(row, "raw_json", None) or {}) or str(row.order_status or "")
        if wanted_label == "all except acknowledged":
            return not row_is_acknowledged(row, acknowledged_ids or set(), ack_overrides)
        if acknowledged_ids is not None:
            # Zoho's Acknowledged custom view also lists orders that were later voided; those
            # are not workable (they can't be assigned or acknowledged), so leave them out.
            return row_is_acknowledged(row, acknowledged_ids, ack_overrides) and actual_status.strip().lower() not in {"void", "cancelled", "canceled"}
        return actual_status.strip().lower().replace("_", " ") == wanted_label
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
    # Inventory tab / its exports (no assignment filter) never lists On Hold orders; Load Planning and Confirmed SO pass an assignment filter and are untouched.
    hide_on_hold = assignment is None
    return [row for row in rows if assignment_match(row) and not (hide_on_hold and live_sales_order_cache.is_on_hold(row)) and not (assignment == "assigned" and is_delivered(getattr(row, "raw_json", None) or {}) and row.assignment_status != "completed") and matches_status(row) and (not wanted_cities or (_shipping_city(row) or "").casefold() in wanted_cities) and (not vehicle_needle or vehicle_needle in str(row.vehicle_id or "").casefold()) and (not customer_needle or customer_needle in str(row.customer_name or "").casefold()) and (not delivery_needle or delivery_needle in row_delivery_status(row).casefold()) and (not needle or needle in " ".join(str(x or "") for x in (row.salesorder_number, row.customer_name, row.reference_number, row.vehicle_id, _shipping_city(row) or "")).lower())]


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
    # _email_rows can call Zoho for order detail - keep it off the event loop.
    rows = await run_in_threadpool(_email_rows, db, body)
    return await generate_sales_order_email_draft(_email_context(rows, body))


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post("/email/send")
def email_send(body: EmailSendRequest, db: Session = Depends(get_db)):
    """Sends the sales-order email straight through the Gmail API (no n8n). Plain `def` so
    FastAPI runs the Zoho hydration, PDF/Excel building and the Gmail call in a worker thread."""
    to = body.to.strip()
    if not to or "@" not in to:
        raise HTTPException(422, "Enter a valid recipient email address.")
    if not gmail_sender.configured():
        raise HTTPException(503, "Gmail is not configured on the server (GMAIL_COMMS_* credentials).")
    rows = _email_rows(db, body)
    export_rows = _confirmed_export_rows(rows) if body.assignment == "assigned" else [line for order in rows for line in flatten_order(order)]
    layout = "confirmed" if body.assignment == "assigned" else "default"
    start = body.date_from or datetime.now(PHT).date().isoformat()
    end = body.date_to or start
    stamp = _download_stamp()
    caption = f"{start} to {end} - {body.status or 'All'} statuses - Downloaded {stamp.replace('_', ' ')} PHT"
    pdf = make_pdf(export_rows, caption, layout).getvalue()
    excel = make_excel(export_rows, caption, layout).getvalue()
    try:
        sent = gmail_sender.send_email(
            to=to,
            subject=body.subject.strip(),
            html=body.htmlBody,
            attachments=[("SalesOrders.pdf", "application/pdf", pdf), ("SalesOrders.xlsx", XLSX_MIME, excel)],
            purpose="sales-order-email",
        )
    except gmail_sender.GmailSendError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    return {"success": True, "messageId": sent["id"], "threadId": sent["threadId"], "orderCount": len(rows), "message": f"Email sent to {to} with {len(rows)} order(s) attached."}


def _confirmed_export_rows(rows: list) -> list[list]:
    """Confirmed SO export: one row per line item with the same columns the screen shows -
    including per-item Mets/Glacier stock, warehouse, notes, truck and driver/helper."""
    item_ids = confirmed_item_ids(rows)
    stock: dict[str, dict] = {}
    if item_ids:
        # Items already seen in the list are cached, so this is normally instant; anything
        # missing is fetched here with a bounded fan-out (the Zoho limiter paces it).
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {item_id: pool.submit(contextvars.copy_context().run, fetch_item_stock, item_id) for item_id in item_ids}
            stock = {item_id: future.result() for item_id, future in futures.items()}

    def staff_name(staff_id) -> str | None:
        member = staff_directory_cache.get_by_id(staff_id, retry_on_miss=False) if staff_id else None
        return (member or {}).get("name")

    def truck_driver(order) -> tuple[str, str]:
        names = [staff_name(getattr(order, "driver_id", None))] + [staff_name(helper) for helper in (getattr(order, "helper_ids", None) or [])]
        return order.vehicle_id or "", " / ".join(dict.fromkeys(name for name in names if name))

    return [line for order in rows for line in flatten_confirmed_order(order, stock.get, truck_driver)]


def _export_payload(db: Session, rows: list, assignment: str | None) -> tuple[list[list], str]:
    rows = _hydrate_export_rows(db, rows)
    if assignment == "assigned":
        return _confirmed_export_rows(rows), "confirmed"
    return [line for order in rows for line in flatten_order(order)], "default"


@router.get("/inventory/export/excel")
def export_excel(date_from: str | None = Query(None), date_to: str | None = Query(None), status: str | None = Query(None), search: str | None = Query(None), assignment: str | None = Query(None), db: Session = Depends(get_db)):
    export_rows, layout = _export_payload(db, _filtered_rows(db, date_from, date_to, status, search, assignment), assignment); start = date_from or datetime.now(PHT).date().isoformat(); end = date_to or start; downloaded = _download_stamp(); caption = f"{start} to {end} - {status or 'All'} statuses - Downloaded {downloaded.replace('_', ' ')} PHT"
    return StreamingResponse(make_excel(export_rows, caption, layout), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="RGF_SalesOrders_{start}_to_{end}_downloaded_{downloaded}.xlsx"'})


@router.get("/inventory/export/pdf")
def export_pdf(date_from: str | None = Query(None), date_to: str | None = Query(None), status: str | None = Query(None), search: str | None = Query(None), assignment: str | None = Query(None), db: Session = Depends(get_db)):
    export_rows, layout = _export_payload(db, _filtered_rows(db, date_from, date_to, status, search, assignment), assignment); start = date_from or datetime.now(PHT).date().isoformat(); end = date_to or start; downloaded = _download_stamp(); caption = f"{start} to {end} - {status or 'All'} statuses - Downloaded {downloaded.replace('_', ' ')} PHT"
    return StreamingResponse(make_pdf(export_rows, caption, layout), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="RGF_SalesOrders_{start}_to_{end}_downloaded_{downloaded}.pdf"'})


def _run_sales_order_sync(start: date | None, end: date | None) -> None:
    """The "Refresh" button: force-invalidate the live Zoho window cache (and every
    currently-assigned SO's cached Zoho data) so the next read re-pulls fresh from Zoho.
    No Neon writes here at all any more - Zoho is re-fetched live, on demand."""
    try:
        live_sales_order_cache.invalidate_windows()
        live_sales_order_cache.invalidate_assigned_zoho()
        _invalidate_ack_cache()
        item_detail_cache.mark_all_stale()
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


def _lock_result_payload(salesorder_id: str, result: dict, *, so_number: str | None = None) -> dict:
    payload = {
        "so_id": salesorder_id,
        "so_number": so_number,
        "locked": bool(result.get("locked")),
        "already_locked": bool(result.get("already_locked")),
        "lock_status": result.get("lock_status"),
        "lock_error": result.get("lock_error"),
    }
    if result.get("message"):
        payload["lock_message"] = result.get("message")
    return payload


def _lock_after_acknowledge(salesorder_id: str, so_number: str | None, user: CurrentUser | None) -> dict:
    try:
        result = lock_salesorder(salesorder_id, user=(user.full_name if user else None))
    except Exception as exc:  # the acknowledge already succeeded and must be kept
        logger.error("[ZOHO_SO_LOCK] unexpected lock error so_number=%s type=%s", so_number or salesorder_id, type(exc).__name__)
        result = {"locked": False, "lock_error": "lock_unexpected_error"}
    payload = _lock_result_payload(salesorder_id, result, so_number=so_number)
    if not payload["locked"]:
        logger.error("[ZOHO_SO_LOCK] acknowledge succeeded but lock failed so_number=%s error=%s", so_number or salesorder_id, payload.get("lock_error"))
    return payload


@router.post("/inventory/sales-orders/{salesorder_id}/acknowledge")
def acknowledge_sales_order_route(salesorder_id: str, current_user: CurrentUser = Depends(require_role("admin", "dispatcher", "warehouse"))):
    cached = live_sales_order_cache.find_cached(salesorder_id) or live_sales_order_cache.ensure_zoho_data(salesorder_id)
    if cached is None:
        raise HTTPException(404, "Sales order was not found.")
    current_status = str(cached.order_status or "").lower().replace("_", " ")
    if current_status in {"void", "cancelled", "canceled"}:
        raise HTTPException(409, "This sales order is locked and cannot be acknowledged.")
    if _is_acknowledged(cached):
        _set_acknowledged(salesorder_id, True)  # Zoho already says acknowledged; keep every list consistent with it
        lock_payload = _lock_after_acknowledge(salesorder_id, cached.salesorder_number, current_user)
        return {"acknowledged": True, "already_acknowledged": True, **lock_payload}  # retry path: no new acknowledge
    try:
        result = acknowledge_sales_order(salesorder_id)
        # Reflect the change on every cached copy instead of re-pulling and re-hydrating the
        # whole window (that re-pull is what made acknowledging take over a minute).
        live_sales_order_cache.mark_acknowledged(salesorder_id, True)
        _set_acknowledged(salesorder_id, True)
        lock_payload = _lock_after_acknowledge(salesorder_id, cached.salesorder_number, current_user)
        return {**result, "acknowledged": True, **lock_payload}
    except ZohoError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/salesorders/{salesorder_id}/lock")
def lock_sales_order_route(salesorder_id: str, current_user: CurrentUser = Depends(require_role("admin", "dispatcher", "warehouse"))):
    """Retry the Zoho lock for ONE already-acknowledged sales order."""
    cached = live_sales_order_cache.find_cached(salesorder_id)  # cache only: no extra Zoho call
    payload = _lock_after_acknowledge(salesorder_id, getattr(cached, "salesorder_number", None), current_user)
    return payload


@router.post("/inventory/sales-orders/{salesorder_id}/remove-acknowledge")
@zoho_acquisition.operation("remove-acknowledgement")
def remove_acknowledge_sales_order_route(salesorder_id: str):
    cached = live_sales_order_cache.find_cached(salesorder_id) or live_sales_order_cache.ensure_zoho_data(salesorder_id)
    if cached is None:
        raise HTTPException(404, "Sales order was not found.")
    if not _is_acknowledged(cached):
        raise HTTPException(409, "This sales order is not acknowledged.")
    try:
        remove_acknowledge_sales_order(salesorder_id)
        epoch = zoho_acquisition.generation()
        detail = fetch_sales_order_detail(salesorder_id)
        live_sales_order_cache.publish_zoho_data(salesorder_id, detail, epoch)
        live_sales_order_cache.mark_acknowledged(salesorder_id, False)
        _set_acknowledged(salesorder_id, False)
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
    current_user: CurrentUser = Depends(require_role("admin", "dispatcher", "warehouse")),
):
    rows = _filtered_rows(db, date_from, date_to, status, search)
    eligible = [row for row in rows if str(row.order_status or "").lower().replace("_", " ") not in {"void", "cancelled", "canceled"} and not _is_acknowledged(row)]
    acknowledged = 0
    failed = 0
    results = []
    for row in eligible:
        try:
            acknowledge_sales_order(str(row.id))
            live_sales_order_cache.mark_acknowledged(str(row.id), True)
            _set_acknowledged(str(row.id), True)
            lock_payload = _lock_after_acknowledge(str(row.id), row.salesorder_number, current_user)
            results.append({"acknowledged": True, **lock_payload})
            acknowledged += 1
        except ZohoError:
            failed += 1
            results.append({"so_id": str(row.id), "so_number": row.salesorder_number, "acknowledged": False, "locked": False, "lock_error": None, "acknowledge_failed": True})
    return {"filtered_count": len(rows), "eligible_count": len(eligible), "acknowledged_count": acknowledged, "failed_count": failed, "results": results}


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
        # Lock state comes from the detail record just fetched (lock_details): opening the drawer
        # stays ONE Zoho GET. The lock credential is used only by the lock flows (lock_salesorder).
        detail["zoho_lock"] = lock_status_from_record(record)
        return {"cached": False, "delivery_status": sales_order_delivery_status(record), **detail}
    except ZohoError as exc:
        if cached and cached.raw_json:
            return {"cached": True, "synced_at": cached.synced_at, **cached.raw_json}
        raise HTTPException(502, str(exc)) from exc
