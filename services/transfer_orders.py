"""Zoho Inventory transfer orders (TO) for Load Planning.

Only DRAFT transfer orders on the Mets <-> Glacier route (both directions) are ever shown or assigned. Warehouses are matched by their Zoho
warehouse IDs (services/branches.py STOCK_RULES, env-overridable) - never by name. Every other warehouse (e.g. Production Area RC) is dropped
right after the fetch.

Live data, memory only (nothing here is written to Neon; the assignment itself is written by the existing assignment flow):
  * the Draft Mets<->Glacier list is pulled from Zoho on first load, after the TTL, and on the "Hard refresh" button (force=True);
  * a TO's line items (and so its kg) are fetched lazily, in the background, only for orders that are being shown, and kept
    until Zoho reports a newer last_modified_time.

A TO is exposed as a SalesOrderCache-shaped row (id "TO:<zoho id>", order_type "TO", number = the TO#, customer_name = the destination
warehouse) so assignment, Fleet, the driver app and the trip workflow work on it unchanged.
"""
from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from models.inventory import SalesOrderCache
from services import branches, order_types, warehouses
from services.item_weight import calculate_line_weight_kg
from services.pack_units import is_pack_unit
from services.serialize import row_to_dict
from services.zoho_client import ZohoError, fetch_transfer_order_detail, fetch_transfer_orders_page

logger = logging.getLogger("transfer_orders")

PREFIX = "TO:"
PHT = ZoneInfo("Asia/Manila")
LIST_TTL_SECONDS = int(os.environ.get("TRANSFER_ORDER_TTL_SECONDS", "300"))
LOOKBACK_DAYS = int(os.environ.get("TRANSFER_ORDER_LOOKBACK_DAYS", "60"))  # the list is date-sorted; paging stops once it is older than this
MAX_PAGES = int(os.environ.get("TRANSFER_ORDER_MAX_PAGES", "10"))
DETAIL_FILL_LIMIT = 60  # line-detail fetches queued per list request

# Display address of each warehouse (a TO is delivered TO the destination warehouse).
_ADDRESS = {
    "mets": {"company_name": "Mets Cold Storage", "address": "Governors Drive, Brgy. Bancal", "city": "Carmona", "state": "Cavite", "country": "Philippines"},
    "glacier": {"company_name": "Glacier Cold Storage", "address": "Amvel Business Park, Ninoy Aquino Ave", "city": "Parañaque City", "country": "Philippines"},
}


def _env_ids(name: str, default: set[str]) -> set[str]:
    raw = os.environ.get(name, "")
    ids = {part.strip() for part in raw.split(",") if part.strip()}
    return ids or default


def _default_ids(key: str) -> set[str]:
    return {str(rule[key]) for rule in branches.STOCK_RULES.values() if rule.get(key)}


# Zoho warehouse IDs per side. Authoritative (the same IDs the stock columns use); override with TO_METS_WAREHOUSE_IDS / TO_GLACIER_WAREHOUSE_IDS.
METS_WAREHOUSE_IDS = _env_ids("TO_METS_WAREHOUSE_IDS", _default_ids("mets"))
GLACIER_WAREHOUSE_IDS = _env_ids("TO_GLACIER_WAREHOUSE_IDS", _default_ids("glacier"))


def _s(value) -> str:
    return "" if value is None else str(value).strip()


def is_to_id(value) -> bool:
    return _s(value).startswith(PREFIX)


def zoho_id(row_id) -> str:
    text = _s(row_id)
    return text[len(PREFIX):] if text.startswith(PREFIX) else text


def row_id(transfer_order_id) -> str:
    return PREFIX + zoho_id(transfer_order_id)


def order_type_of(row) -> str:
    """'SO' | 'WM' | 'PO' | 'TO' (services/order_types.py). Works for live rows, Neon history rows and plain ids."""
    return order_types.order_type_of(row)


def warehouse_side(warehouse_id) -> str | None:
    """'mets' / 'glacier' for a Zoho warehouse ID, else None. IDs only - names are never consulted."""
    wid = _s(warehouse_id)
    if wid in METS_WAREHOUSE_IDS:
        return "mets"
    if wid in GLACIER_WAREHOUSE_IDS:
        return "glacier"
    return None


def route_of(record: dict) -> tuple[str | None, str | None]:
    return warehouse_side(record.get("from_warehouse_id")), warehouse_side(record.get("to_warehouse_id"))


def is_draft(record: dict) -> bool:
    """Draft ONLY (agreed rule). Approved, In Transit, Transferred, Partially Transferred, Pending and Void are never shown or assigned."""
    return _s(record.get("status")).casefold().replace(" ", "_") == "draft"


def is_wanted(record: dict) -> bool:
    """Draft AND (Mets -> Glacier OR Glacier -> Mets). Anything else (other warehouses, other statuses) is excluded."""
    return is_draft(record) and route_of(record) in {("mets", "glacier"), ("glacier", "mets")}


def _record_id(record: dict) -> str:
    return _s(record.get("transfer_order_id") or record.get("id"))


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ---- list cache (memory) -----------------------------------------------------------------------------------------

_lock = threading.RLock()
_fetch_lock = threading.Lock()
_list: dict = {"records": None, "at": 0.0, "refreshed_at": None}
_details: dict[str, tuple[str, dict]] = {}  # transfer order id -> (last_modified_time it was fetched for, detail record)
_inflight: set[str] = set()
_failed: dict[str, float] = {}
_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="to-detail")
FAILURE_BACKOFF_SECONDS = 60


def _pull() -> dict[str, dict]:
    floor = datetime.now(PHT).date() - timedelta(days=LOOKBACK_DAYS)
    wanted: dict[str, dict] = {}
    seen = not_draft = off_route = 0
    statuses: dict[str, int] = {}
    unknown_warehouses: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        payload = fetch_transfer_orders_page(page=page, per_page=200)
        records = [r for r in (payload.get("transfer_orders") or payload.get("transferorders") or []) if isinstance(r, dict)]
        for record in records:
            rid = _record_id(record)
            seen += 1
            status = _s(record.get("status")).casefold()
            statuses[status] = statuses.get(status, 0) + 1
            if rid and is_wanted(record):
                wanted[rid] = record
            elif not is_draft(record):
                not_draft += 1
            else:
                off_route += 1
                unknown_warehouses.update(w for w in (_s(record.get("from_warehouse_id")), _s(record.get("to_warehouse_id"))) if w and not warehouse_side(w))
        has_more = (payload.get("page_context") or {}).get("has_more_page")
        if isinstance(has_more, str):
            has_more = has_more.strip().lower() == "true"
        dates = [d for d in (_as_date(r.get("date")) for r in records) if d]
        if not records or not has_more or (dates and min(dates) < floor):
            break
    logger.info("[TRANSFER_ORDERS] pull: %d seen, %d kept, %d not draft, %d draft but off Mets<->Glacier; statuses=%s; unmatched warehouse ids=%s",
                seen, len(wanted), not_draft, off_route, statuses, sorted(unknown_warehouses))
    return wanted


def get_records(*, force: bool = False) -> tuple[dict[str, dict], datetime]:
    """The Draft Mets<->Glacier records. Fetched from Zoho when there is none, when the TTL passed, or when `force` (Hard refresh)."""
    def fresh() -> bool:
        return _list["records"] is not None and time.monotonic() - _list["at"] < LIST_TTL_SECONDS

    with _lock:
        if not force and fresh():
            return _list["records"], _list["refreshed_at"]
    with _fetch_lock:  # single flight: concurrent first loads share one pull
        with _lock:
            if not force and fresh():
                return _list["records"], _list["refreshed_at"]
        records = _pull()
        now = datetime.now(timezone.utc)
        with _lock:
            _list.update(records=records, at=time.monotonic(), refreshed_at=now)
        logger.info("[TRANSFER_ORDERS] list %s: %d Draft Mets<->Glacier order(s)", "hard-refreshed" if force else "loaded", len(records))
        return records, now


def last_refreshed_at() -> datetime | None:
    with _lock:
        return _list["refreshed_at"]


def reset() -> None:
    """Test hook."""
    with _lock:
        _list.update(records=None, at=0.0, refreshed_at=None)
        _details.clear()
        _inflight.clear()
        _failed.clear()


# ---- detail (lines) ----------------------------------------------------------------------------------------------


def _modified(record: dict | None) -> str:
    return _s((record or {}).get("last_modified_time"))


def cached_detail(tid: str, record: dict | None = None) -> dict | None:
    with _lock:
        hit = _details.get(tid)
    if hit is None:
        return None
    modified = _modified(record)
    if modified and hit[0] and hit[0] != modified:
        return None  # Zoho changed the order since the lines were read
    return hit[1]


def _store_detail(tid: str, modified: str, detail: dict) -> None:
    with _lock:
        _details[tid] = (modified, detail)
        _failed.pop(tid, None)


def fetch_detail(tid: str, record: dict | None = None) -> dict:
    """Fetch the lines now (one Zoho call) and cache them."""
    payload = fetch_transfer_order_detail(tid)
    detail = payload.get("transfer_order") if isinstance(payload, dict) and isinstance(payload.get("transfer_order"), dict) else payload
    if not isinstance(detail, dict) or not (detail.get("line_items") is not None or detail.get("transfer_order_id") or detail.get("transfer_order_number")):
        raise ZohoError("Zoho returned no transfer order detail.")
    _store_detail(tid, _modified(record) or _modified(detail), detail)
    return detail


def _fill_worker(tid: str, record: dict | None) -> None:
    try:
        fetch_detail(tid, record)
    except ZohoError as exc:
        logger.warning("[TRANSFER_ORDERS] detail %s unavailable: %s", tid, exc)
        with _lock:
            _failed[tid] = time.monotonic()
    finally:
        with _lock:
            _inflight.discard(tid)


def request_details(records: list[dict]) -> int:
    """Queue a background line fetch for every listed order whose lines are not cached. Returns how many are still unknown (and not in a
    failure back-off) - the caller reports details_pending while that is > 0."""
    now = time.monotonic()
    waiting = 0
    start: list[dict] = []
    with _lock:
        for record in records:
            tid = _record_id(record)
            if cached_detail(tid, record) is not None:
                continue
            failed_at = _failed.get(tid)
            if failed_at is not None and now - failed_at < FAILURE_BACKOFF_SECONDS:
                continue
            waiting += 1
            if tid not in _inflight and len(start) < DETAIL_FILL_LIMIT:
                _inflight.add(tid)
                start.append(record)
    for record in start:
        _pool.submit(contextvars.copy_context().run, _fill_worker, _record_id(record), record)
    return waiting


# ---- row building ------------------------------------------------------------------------------------------------


def _person(record: dict, key: str) -> str | None:
    for candidate in (f"{key}_name", key, f"{key}_by", f"{key}_user_name"):
        value = record.get(candidate)
        if isinstance(value, dict):
            value = value.get("name") or value.get("user_name")
        if _s(value):
            return _s(value)
    return None


def _lines(detail: dict | None, source_name: str) -> list[dict]:
    lines = []
    for item in (detail or {}).get("line_items") or []:
        if not isinstance(item, dict):
            continue
        quantity = item.get("quantity_transfer")
        if quantity is None:
            quantity = item.get("quantity")
        lines.append({**item, "quantity": quantity, "unit": item.get("unit") or item.get("unit_name") or item.get("usage_unit"), "name": item.get("name") or item.get("item_name") or item.get("description"), "warehouse_name": source_name})
    return lines


def build_row(record: dict, detail: dict | None = None) -> SalesOrderCache:
    """A transfer order as an in-memory SalesOrderCache-shaped row (never added to a DB session)."""
    merged = {**record, **{k: v for k, v in (detail or {}).items() if v not in (None, "", [], {})}}
    tid = _record_id(merged) or _record_id(record)
    source_side, dest_side = route_of(merged)
    source_name = _s(merged.get("from_warehouse_name")) or (warehouses.WAREHOUSES[source_side]["name"] if source_side else "")
    dest_name = _s(merged.get("to_warehouse_name")) or (warehouses.WAREHOUSES[dest_side]["name"] if dest_side else "")
    number = _s(merged.get("transfer_order_number")) or tid
    day = _as_date(merged.get("date"))
    address = dict(_ADDRESS.get(dest_side or "", {"company_name": dest_name}))
    raw = {
        "order_type": "TO",
        "transfer_order_id": tid,
        "transfer_order_number": number,
        "salesorder_number": number,
        "customer_name": dest_name,
        "status": "draft",
        "reason": _s(merged.get("reason")) or None,
        "date": day.isoformat() if day else None,
        "shipment_date": day.isoformat() if day else None,
        "from_warehouse_id": _s(merged.get("from_warehouse_id")),
        "from_warehouse_name": source_name,
        "to_warehouse_id": _s(merged.get("to_warehouse_id")),
        "to_warehouse_name": dest_name,
        "source_warehouse_key": source_side,
        "destination_warehouse_key": dest_side,
        "created_by": _person(merged, "created_by"),
        "last_modified_by": _person(merged, "last_modified_by"),
        "last_modified_time": _s(merged.get("last_modified_time")) or None,
        "quantity_transfer": merged.get("quantity_transfer"),
        "shipping_address": address,
        "notes": _s(merged.get("notes")) or None,
        "line_items": _lines(detail, source_name),
        "lines_loaded": detail is not None,
    }
    row = SalesOrderCache(
        id=row_id(tid), salesorder_number=number, reference_number=None, customer_name=dest_name, order_status="draft",
        order_date=day, expected_shipment_date=day, total=None, billing_address={}, shipping_address=address, raw_json=raw,
        synced_at=datetime.now(timezone.utc), assignment_status="unassigned",
    )
    row.order_type = "TO"
    row.source_warehouse = source_name
    row.destination_warehouse = dest_name
    return row


def list_rows(start: date, end: date, *, force: bool = False) -> tuple[list[SalesOrderCache], datetime, int]:
    """(rows whose TO date is in [start, end], when the list was last pulled, number of orders whose lines are still loading)."""
    records, refreshed = get_records(force=force)
    in_range = [r for r in records.values() if (d := _as_date(r.get("date"))) is not None and start <= d <= end]
    in_range.sort(key=lambda r: (_s(r.get("date")), _s(r.get("transfer_order_number"))))
    pending = request_details(in_range)
    rows = [build_row(r, cached_detail(_record_id(r), r)) for r in in_range]
    return rows, refreshed, pending


def ensure_row(order_id: str, *, validate: bool = False) -> SalesOrderCache:
    """Row with its lines for an id, fetching the detail from Zoho (one call) when it is not in memory. `validate` (assigning a fresh TO)
    re-checks Draft + Mets<->Glacier; an already-assigned TO is loaded without it, whatever Zoho says now."""
    tid = zoho_id(order_id)
    with _lock:
        record = (_list["records"] or {}).get(tid)
    detail = cached_detail(tid, record)
    if detail is None:
        detail = fetch_detail(tid, record)
    base = {**(record or {}), **detail}
    if validate and not is_wanted(base):
        raise NotAssignable(f"{base.get('transfer_order_number') or tid} is not a Draft Mets <-> Glacier transfer order.")
    return build_row(record or detail, detail)


class NotAssignable(Exception):
    pass


# ---- API summary -------------------------------------------------------------------------------------------------


def _number(item, key) -> float:
    try:
        return float(item.get(key)) if item.get(key) is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def line_weights(row, *, allow_fetch: bool = False) -> list[float | None]:
    raw = row.raw_json or {}
    return [calculate_line_weight_kg(item.get("quantity"), item.get("unit"), item.get("item_id"), item=item, context=f"TO={row.salesorder_number}", allow_fetch=allow_fetch) for item in raw.get("line_items") or []]


def total_weight_kg(rows) -> tuple[float, bool, list[str]]:
    """(total kg across the rows, complete?, item ids whose package weight is still unknown). Complete only when every order's lines are
    loaded and every line has a weight - the same 'kg+' rule as sales orders. Cache-only: never calls Zoho."""
    total, complete, missing = 0.0, True, []
    for row in rows:
        raw = row.raw_json or {}
        if not raw.get("lines_loaded"):
            complete = False
            continue
        for item, weight in zip(raw.get("line_items") or [], line_weights(row)):
            if weight is None:
                complete = False
                if item.get("item_id"):
                    missing.append(str(item["item_id"]))
            else:
                total += weight
    return round(total, 3), complete, missing


def summary(row, *, allow_fetch: bool = False) -> dict:
    """The list/Confirmed JSON for a transfer order - same keys as a sales-order summary plus the TO fields (see CONTRACT.md)."""
    from services import staff_directory_cache

    result = row_to_dict(row, exclude={"raw_json"})
    raw = row.raw_json or {}
    result["id"] = row.id
    if result.get("total") is not None:
        result["total"] = float(result["total"])
    if getattr(row, "driver_id", None):
        driver = staff_directory_cache.get_by_id(row.driver_id)
        result["driver_name"] = driver["name"] if driver else None
    address = raw.get("shipping_address") or row.shipping_address or {}
    result["order_type"] = "TO"
    result["transfer_order_number"] = raw.get("transfer_order_number") or row.salesorder_number
    result["reason"] = raw.get("reason")
    result["source_warehouse"] = raw.get("from_warehouse_name") or getattr(row, "source_warehouse", None)
    result["destination_warehouse"] = raw.get("to_warehouse_name") or getattr(row, "destination_warehouse", None)
    result["source_warehouse_key"] = raw.get("source_warehouse_key")
    result["destination_warehouse_key"] = raw.get("destination_warehouse_key")
    result["created_by"] = raw.get("created_by")
    result["last_modified_by"] = raw.get("last_modified_by")
    result["driver_ids"] = list(getattr(row, "helper_ids", None) or ([row.driver_id] if getattr(row, "driver_id", None) else []))
    result["assignment_batch_id"] = getattr(row, "assignment_batch_id", None)
    result["email_status"] = getattr(row, "email_status", None)
    result["email_error"] = getattr(row, "email_error", None)
    sent_at = getattr(row, "email_sent_at", None)
    result["email_sent_at"] = sent_at.isoformat() if hasattr(sent_at, "isoformat") else sent_at
    result["shipping_address"] = address
    result["shipping_city"] = (address or {}).get("city")
    result["zoho_lock"] = None
    result["branch_id"] = result["branch_name"] = result["branch_code"] = None
    result["notes"] = raw.get("notes")
    result["mets_qty_available_for_sale"] = None
    result["glacier_qty_available_for_sale"] = None
    products, packs, cases, units = [], 0.0, 0.0, 0.0
    lines = raw.get("line_items") or []
    for item, weight in zip(lines, line_weights(row, allow_fetch=allow_fetch)):
        quantity = _number(item, "quantity")
        unit = _s(item.get("unit"))
        normalized = unit.casefold()
        if is_pack_unit(normalized):
            packs += quantity
        elif "case" in normalized or "carton" in normalized:
            cases += quantity
        else:
            units += quantity
        products.append({"mets_qty_available_for_sale": None, "glacier_qty_available_for_sale": None, "line_item_id": item.get("line_item_id"), "item_id": item.get("item_id"), "name": item.get("name"), "sku": item.get("sku") or item.get("item_id"), "quantity": quantity, "unit": unit or None, "total_weight_kg": weight, "packaging_type": "pack" if is_pack_unit(normalized) else "case" if "case" in normalized or "carton" in normalized else None, "pack_quantity": quantity if is_pack_unit(normalized) else 0, "case_quantity": quantity if "case" in normalized or "carton" in normalized else 0, "quantity_packed": 0.0, "quantity_shipped": 0.0})
    result["product_count"] = len([p for p in products if p["name"]])
    result["products"] = products
    result["pack_count"] = packs
    result["case_count"] = cases
    result["unit_count"] = units
    try:
        list_quantity = float(raw.get("quantity_transfer")) if raw.get("quantity_transfer") is not None else None
    except (TypeError, ValueError):
        list_quantity = None
    result["total_item_quantity"] = sum(_number(item, "quantity") for item in lines) if lines else (list_quantity or 0.0)
    result["lines_loaded"] = bool(raw.get("lines_loaded"))
    return result
