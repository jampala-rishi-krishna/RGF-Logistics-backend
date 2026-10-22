"""Zoho Inventory purchase orders (PO) for Load Planning.

Rule (Rishi, 2026-10-16): ONLY numbers starting "PO-" (e.g. PO-1026-01704). "SS PO-..." numbers and anything of the SariSuki Store Inc.
branch are dropped right after the fetch - by number prefix AND by branch. The pickup list requires Issued, cf_shipment_method =
For Pickup, delivery_date tomorrow in Manila, and creation before the cutoff (or manager Pahabol approval).

Delivery date = Zoho delivery_date; missing dates are excluded, with no fallback. A PO is picked up at the VENDOR and delivered to its DESTINATION warehouse (Mets / Glacier).

Live data, memory only (nothing here is written to Neon; the assignment itself is written by the existing assignment flow):
  * the list is pulled from Zoho on first load, after the TTL, and on the "Hard refresh" button (force=True);
  * a PO's line items (and so its kg and the vendor address) are fetched lazily, in the background, only for orders that are being shown,
    and kept until Zoho reports a newer last_modified_time.

A PO is exposed as a SalesOrderCache-shaped row (id "PO:<zoho id>", order_type "PO", number = the PO#, customer_name = the vendor,
shipping_address = the vendor (pickup) address) so assignment, Fleet, the driver app and the trip workflow work on it unchanged.
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
from services import branches, memory_debug, transfer_orders, warehouses, zoho_resilience, pickup_rules
from services import order_fields
from services.item_weight import calculate_line_weight_kg
from services.pack_units import is_pack_unit
from services.serialize import row_to_dict
from services.zoho_client import ZohoError, fetch_purchase_order_detail, fetch_purchase_orders_page, fetch_vendor_contact

logger = logging.getLogger("purchase_orders")

PREFIX = "PO:"
NUMBER_PREFIX = "PO-"
PHT = ZoneInfo("Asia/Manila")
LIST_TTL_SECONDS = int(os.environ.get("PURCHASE_ORDER_TTL_SECONDS", "300"))
LOOKBACK_DAYS = int(os.environ.get("PURCHASE_ORDER_LOOKBACK_DAYS", "60"))  # the list is PO-date sorted; paging stops once it is older than this
MAX_PAGES = int(os.environ.get("PURCHASE_ORDER_MAX_PAGES", "10"))
DETAIL_FILL_LIMIT = 60
FAILURE_BACKOFF_SECONDS = 60
SARISUKI_BRANCH_ID = next(b["id"] for b in branches.BRANCHES if b["code"] == "SSI")
HIDDEN_BY_DEFAULT = frozenset({"cancelled", "canceled", "closed"})

_ADDRESS = transfer_orders._ADDRESS  # display address of each warehouse (a PO is delivered TO its destination warehouse)


def _s(value) -> str:
    return "" if value is None else str(value).strip()


def _squash(value) -> str:
    return "".join(ch for ch in _s(value).casefold() if ch.isalnum())


def is_po_id(value) -> bool:
    return _s(value).startswith(PREFIX)


def zoho_id(row_id) -> str:
    text = _s(row_id)
    return text[len(PREFIX):] if text.startswith(PREFIX) else text


def row_id(purchaseorder_id) -> str:
    return PREFIX + zoho_id(purchaseorder_id)


def normalize_status(value) -> str:
    """'Partially Received' / 'partially_received' -> 'partially_received'."""
    return _s(value).casefold().replace(" ", "_").replace("-", "_")


def status_label(value) -> str:
    return normalize_status(value).replace("_", " ").title() or "-"


def _number(record: dict) -> str:
    return _s(record.get("purchaseorder_number") or record.get("purchase_order_number"))


def is_sarisuki(record: dict) -> bool:
    return _s(record.get("branch_id") or record.get("location_id")) == SARISUKI_BRANCH_ID or "sarisuki" in _squash(record.get("branch_name"))


def is_wanted(record: dict) -> bool:
    """Number starts with "PO-" (so "SS PO-..." never passes) AND the branch is not SariSuki Store Inc."""
    return _number(record).upper().startswith(NUMBER_PREFIX) and not is_sarisuki(record)


def status_visible(record_or_status, statuses: set[str] | None = None) -> bool:
    """`statuses` = the PO tab's status filter (normalized codes); None = the default: everything except Cancelled and Closed."""
    status = normalize_status(record_or_status.get("status") if isinstance(record_or_status, dict) else record_or_status)
    return status not in HIDDEN_BY_DEFAULT if statuses is None else status in statuses


def parse_statuses(value) -> set[str] | None:
    """'open,billed' -> {'open', 'billed'}; 'all' -> every status; empty -> None (the default rule)."""
    parts = [normalize_status(part) for part in (value if isinstance(value, str) else "").split(",") if part.strip()]
    if not parts:
        return None
    return {"*"} if "all" in parts else set(parts)


def _record_id(record: dict) -> str:
    return _s(record.get("purchaseorder_id") or record.get("purchase_order_id") or record.get("id"))


_as_date = transfer_orders._as_date


def delivery_day(record: dict) -> tuple[date | None, bool]:
    """(the date the PO is planned on, True when it is the Expected Delivery Date / False when it fell back to the PO date)."""
    expected = _as_date(record.get("delivery_date"))
    if expected is not None:
        return expected, True
    return None, False


def destination(record: dict) -> tuple[str | None, str]:
    """(warehouse side 'mets' / 'glacier' / None, display name) of where the PO is delivered. Zoho warehouse IDs first (the same IDs as
    the TO rule and the stock columns), then the warehouse / location name, on the PO and then on its lines."""
    lines = [item for item in record.get("line_items") or [] if isinstance(item, dict)]
    delivery = record.get("delivery_address") if isinstance(record.get("delivery_address"), dict) else {}
    ids = [record.get(key) for key in ("location_id", "warehouse_id", "delivery_warehouse_id")] + [delivery.get("warehouse_id")]
    ids += [item.get(key) for item in lines for key in ("warehouse_id", "location_id")]
    named = [_s(n) for n in [record.get(key) for key in ("location_name", "warehouse_name", "delivery_warehouse_name")] + [delivery.get("warehouse_name")]
             + [item.get(key) for item in lines for key in ("warehouse_name", "location_name")] if _s(n)]
    hints = named + [_s(delivery.get(key)) for key in ("attention", "organization_address", "address") if _s(delivery.get(key))]
    for wid in ids:
        side = transfer_orders.warehouse_side(wid)
        if side:
            return side, next((n for n in named if warehouses.warehouse_key(n) == side), warehouses.WAREHOUSES[side]["name"])
    for hint in hints:
        side = warehouses.warehouse_key(hint)
        if side:
            return side, hint if hint in named else warehouses.WAREHOUSES[side]["name"]
    return None, named[0] if named else ""


_vendor_lock = threading.RLock()
_vendors: dict[str, tuple[float, dict]] = {}
_decisions: dict[str, str] = {}


def load_decisions(db):
    from models.access_workflow import PickupDecision
    from sqlalchemy import select
    with _lock:
        _decisions.update({row.decision_key: row.status for row in db.execute(select(PickupDecision)).scalars()})


def pickup_state(record):
    state = pickup_rules.classify(record)
    if state == "pahabol":
        with _lock:
            decision = _decisions.get(pickup_rules.decision_key(record))
        return "scheduled" if decision == "approved" else "rejected" if decision == "rejected" else state
    return state


def vendor_contact(record, *, allow_fetch: bool = True):
    vid = _s(record.get("vendor_id"))
    if not vid:
        return {}
    # Called only by the detail worker, never by rendering or the summary path.
    with _vendor_lock:
        hit = _vendors.get(vid)
        if hit and time.monotonic() - hit[0] < 3600:
            return hit[1]
        if not allow_fetch:
            return hit[1] if hit else {}
        try:
            contact = fetch_vendor_contact(vid).get("contact") or {}
        except ZohoError:
            logger.warning("[PURCHASE_ORDERS] vendor shipping contact unavailable for %s", vid)
            contact = {}
        if len(_vendors) >= 500:
            _vendors.pop(next(iter(_vendors)))
        _vendors[vid] = (time.monotonic(), contact)
        return contact


def vendor_address(record: dict) -> dict:
    contact = record.get("vendor_contact") or {}
    shipping = contact.get("shipping_address") or {}
    return {**shipping, "company_name": record.get("vendor_name")} if any(shipping.get(k) for k in ("address", "street", "city")) else {}


def vendor_person(record):
    contact = record.get("vendor_contact") or {}
    people = contact.get("contact_persons") or []
    person = next((p for p in people if p.get("is_primary_contact")), people[0] if people else {})
    return {"person": " ".join(str(person.get(k) or "") for k in ("first_name", "last_name")).strip(),
            "phone": person.get("mobile") or person.get("phone") or contact.get("mobile") or contact.get("phone") or ""}


# ---- list cache (memory) -----------------------------------------------------------------------------------------

_lock = threading.RLock()
_list: dict = {"records": None, "at": 0.0, "refreshed_at": None, "status_counts": {}, "failed_at": 0.0, "failed_since": None, "error": None}
_details: dict[str, tuple[str, dict]] = {}
_inflight: set[str] = set()
_failed: dict[str, float] = {}
_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="po-detail")
_flight = zoho_resilience.SingleFlight("purchase-orders")
# Line-item details kept in memory: only orders still in the latest list, at most this many (oldest dropped first).
DETAIL_CACHE_MAX = int(os.environ.get("PURCHASE_ORDER_DETAIL_CACHE_MAX", "500"))
LIST_FAILURE_BACKOFF_SECONDS = 60  # after a failed list pull, serve the last good list for this long before asking Zoho again


class Warming(Exception):
    """The first PO list pull is still running after the request deadline; it keeps filling the cache in the background."""


def apply_webhook(record: dict) -> bool:
    """Merge a pushed purchase order into the list/detail caches.

    The webhook body is already a detail-shaped record, so no list or detail Zoho
    request is needed when the PO tab renders it.
    """
    pid = _record_id(record)
    if not pid:
        return False
    modified = _modified(record)
    with _lock:
        old = _details.get(pid)
        if old and modified and old[0] and modified <= old[0]:
            return False
        records = dict(_list["records"] or {})
        records[pid] = {**records.get(pid, {}), **record, "source": "webhook"}
        _list.update(records=records, at=time.monotonic(), refreshed_at=datetime.now(timezone.utc), error=None)
        _details.pop(pid, None)
        _details[pid] = (modified, {**record, "source": "webhook"})
        _prune_details_locked(set(records))
    return True


def _pull() -> tuple[dict[str, dict], dict[str, int]]:
    floor = datetime.now(PHT).date() - timedelta(days=LOOKBACK_DAYS)
    wanted: dict[str, dict] = {}
    seen = ss_number = other_number = sarisuki = 0
    statuses: dict[str, int] = {}
    for page in range(1, MAX_PAGES + 1):
        payload = fetch_purchase_orders_page(page=page, per_page=200)
        records = [r for r in (payload.get("purchaseorders") or payload.get("purchase_orders") or []) if isinstance(r, dict)]
        for record in records:
            seen += 1
            rid = _record_id(record)
            if rid and is_wanted(record):
                wanted[rid] = record
                status = normalize_status(record.get("status"))
                statuses[status] = statuses.get(status, 0) + 1
            elif _number(record).upper().startswith("SS"):
                ss_number += 1
            elif is_sarisuki(record):
                sarisuki += 1
            else:
                other_number += 1
        has_more = (payload.get("page_context") or {}).get("has_more_page")
        if isinstance(has_more, str):
            has_more = has_more.strip().lower() == "true"
        dates = [d for d in (_as_date(r.get("date")) for r in records) if d]
        if not records or not has_more or (dates and min(dates) < floor):
            break
    logger.info("[PURCHASE_ORDERS] pull: %d seen, %d kept (PO-), %d 'SS' numbers dropped, %d SariSuki branch dropped, %d other numbers dropped; "
                "kept statuses=%s", seen, len(wanted), ss_number, sarisuki, other_number, dict(sorted(statuses.items())))
    return wanted, statuses


def _prune_details_locked(keep: set[str] | None = None) -> None:
    """Drop line details of orders no longer in the list (when `keep` is given), expired failure marks, then the oldest over the cap."""
    if keep is not None:
        for pid in [pid for pid in _details if pid not in keep]:
            del _details[pid]
    now = time.monotonic()
    for pid in [pid for pid, at in _failed.items() if now - at >= FAILURE_BACKOFF_SECONDS or (keep is not None and pid not in keep)]:
        del _failed[pid]
    while len(_details) > DETAIL_CACHE_MAX:
        del _details[next(iter(_details))]


def _pull_and_store(force: bool) -> tuple[dict[str, dict], datetime]:
    """One upstream pull (runs on the single-flight thread). Success replaces the list; a failure is remembered for the backoff."""
    try:
        records, statuses = _pull()
    except Exception as exc:
        with _lock:
            _list.update(failed_at=time.monotonic(), failed_since=_list["failed_since"] or datetime.now(timezone.utc), error=zoho_resilience.light_error(exc))
        logger.warning("[PURCHASE_ORDERS] list pull failed (%s): %s", type(exc).__name__, exc)
        raise
    now = datetime.now(timezone.utc)
    with _lock:
        _list.update(records=records, at=time.monotonic(), refreshed_at=now, status_counts=statuses, failed_at=0.0, failed_since=None, error=None)
        _prune_details_locked(set(records))
    logger.info("[PURCHASE_ORDERS] list %s: %d PO- order(s)", "hard-refreshed" if force else "loaded", len(records))
    memory_debug.log_rss(f"po-list-pulled records={len(records)} details={len(_details)}")
    return records, now


def _serve_stale(reason: str) -> tuple[dict[str, dict], datetime]:
    with _lock:
        records, refreshed, since = _list["records"], _list["refreshed_at"], _list["failed_since"]
    zoho_resilience.note_stale(since or refreshed, reason)
    return records, refreshed


def get_records(*, force: bool = False, deadline: float | None = None) -> tuple[dict[str, dict], datetime]:
    """The PO- records (every status). Fetched from Zoho when there is none, when the TTL passed, or when `force` (Hard refresh).

    With a list in memory, a failed or too-slow pull answers with that list and marks the request stale (zoho_resilience.track_stale)
    instead of failing. After a failure the list is not pulled again for LIST_FAILURE_BACKOFF_SECONDS (Hard refresh still may).
    Concurrent pulls - first loads and Hard refreshes alike - share ONE upstream pull. A request waits at most `deadline` seconds
    (default zoho_resilience.REQUEST_DEADLINE_SECONDS); the pull keeps running and fills the cache for the next request."""
    now = time.monotonic()
    with _lock:
        has_data = _list["records"] is not None
        if not force and has_data and now - _list["at"] < LIST_TTL_SECONDS:
            return _list["records"], _list["refreshed_at"]
        remembered = _list["error"]
        backing_off = not force and remembered is not None and now - _list["failed_at"] < LIST_FAILURE_BACKOFF_SECONDS
    if backing_off:
        if has_data:
            return _serve_stale("zoho_budget" if zoho_resilience.is_budget_error(remembered) else "zoho_failed")
        raise zoho_resilience.light_error(remembered)
    try:
        return _flight.run(lambda: _pull_and_store(force), timeout=zoho_resilience.REQUEST_DEADLINE_SECONDS if deadline is None else deadline)
    except zoho_resilience.DeadlineExceeded:
        if has_data:
            return _serve_stale("zoho_slow")
        raise Warming("Purchase orders are loading from Zoho. Try again in a few seconds.") from None
    except ZohoError as exc:
        with _lock:
            has_data = _list["records"] is not None
        if has_data:
            return _serve_stale("zoho_budget" if zoho_resilience.is_budget_error(exc) else "zoho_failed")
        raise


def is_refreshing() -> bool:
    return _flight.running()


def last_refreshed_at() -> datetime | None:
    with _lock:
        return _list["refreshed_at"]


def loaded_status_counts() -> dict[str, int]:
    """Status -> count over every kept PO- record of the last pull (all dates). Memory only."""
    with _lock:
        return dict(_list["status_counts"])


def reset() -> None:
    """Test hook."""
    with _lock:
        _list.update(records=None, at=0.0, refreshed_at=None, status_counts={}, failed_at=0.0, failed_since=None, error=None)
        _details.clear()
        _inflight.clear()
        _failed.clear()
        _decisions.clear()
        _vendors.clear()


# ---- detail (lines) ----------------------------------------------------------------------------------------------


def _modified(record: dict | None) -> str:
    return _s((record or {}).get("last_modified_time"))


def cached_detail(pid: str, record: dict | None = None) -> dict | None:
    with _lock:
        hit = _details.get(pid)
    if hit is None:
        return None
    modified = _modified(record)
    if modified and hit[0] and hit[0] != modified:
        return None  # Zoho changed the order since the lines were read
    return hit[1]


def fetch_detail(pid: str, record: dict | None = None, *, enrich_vendor: bool = True) -> dict:
    """Fetch the lines now (one Zoho call) and cache them."""
    payload = fetch_purchase_order_detail(pid)
    detail = payload.get("purchaseorder") if isinstance(payload, dict) and isinstance(payload.get("purchaseorder"), dict) else payload
    if not isinstance(detail, dict) or not (detail.get("line_items") is not None or detail.get("purchaseorder_id") or detail.get("purchaseorder_number")):
        raise ZohoError("Zoho returned no purchase order detail.")
    detail = {**detail, "vendor_contact": vendor_contact({**(record or {}), **detail}, allow_fetch=enrich_vendor)}
    if not enrich_vendor:
        # Assigned-only Fleet hydration must be one PO detail call. Its row is
        # retained by the assigned cache; don't suppress later Inventory enrichment.
        return detail
    with _lock:
        _details.pop(pid, None)  # re-insert at the end: the oldest entries are the ones dropped over the cap
        _details[pid] = (_modified(record) or _modified(detail), detail)
        _failed.pop(pid, None)
        _prune_details_locked()
    return detail


def _fill_worker(pid: str, record: dict | None) -> None:
    try:
        fetch_detail(pid, record)
    except ZohoError as exc:
        logger.warning("[PURCHASE_ORDERS] detail %s unavailable: %s", pid, exc)
        with _lock:
            _failed[pid] = time.monotonic()
    finally:
        with _lock:
            _inflight.discard(pid)


def request_details(records: list[dict]) -> int:
    """Queue a background line fetch for every shown order whose lines are not cached; returns how many are still unknown."""
    now = time.monotonic()
    waiting = 0
    start: list[dict] = []
    with _lock:
        _prune_details_locked()
        for record in records:
            pid = _record_id(record)
            if cached_detail(pid, record) is not None:
                continue
            failed_at = _failed.get(pid)
            if failed_at is not None and now - failed_at < FAILURE_BACKOFF_SECONDS:
                continue
            waiting += 1
            if pid not in _inflight and len(start) < DETAIL_FILL_LIMIT:
                _inflight.add(pid)
                start.append(record)
    for record in start:
        _pool.submit(contextvars.copy_context().run, _fill_worker, _record_id(record), record)
    return waiting


# ---- row building ------------------------------------------------------------------------------------------------


def _lines(detail: dict | None, destination_name: str) -> list[dict]:
    lines = []
    for item in (detail or {}).get("line_items") or []:
        if not isinstance(item, dict):
            continue
        lines.append({**item, "unit": item.get("unit") or item.get("unit_name") or item.get("usage_unit"), "name": item.get("name") or item.get("item_name") or item.get("description"),
                      "warehouse_name": destination_name or item.get("warehouse_name") or item.get("location_name")})
    return lines


def build_row(record: dict, detail: dict | None = None) -> SalesOrderCache:
    """A purchase order as an in-memory SalesOrderCache-shaped row (never added to a DB session)."""
    merged = {**record, **{k: v for k, v in (detail or {}).items() if v not in (None, "", [], {})}}
    pid = _record_id(merged) or _record_id(record)
    number = _number(merged) or pid
    day, expected = delivery_day(merged)
    po_date = _as_date(merged.get("date"))
    side, dest_name = destination(merged)
    vendor = _s(merged.get("vendor_name")) or None
    pickup = vendor_address(merged)
    branch = branches.describe(merged.get("branch_id"), merged.get("branch_name"))
    try:
        total = float(merged.get("total")) if merged.get("total") not in (None, "") else None
    except (TypeError, ValueError):
        total = None
    raw = {
        "order_type": "PO",
        "purchaseorder_id": pid,
        "purchaseorder_number": number,
        "salesorder_number": number,
        "reference_number": _s(merged.get("reference_number")) or None,
        "vendor_id": _s(merged.get("vendor_id")) or None,
        "vendor_name": vendor,
        "customer_name": vendor,
        "status": normalize_status(merged.get("status")),
        "date": po_date.isoformat() if po_date else None,
        "delivery_date": day.isoformat() if expected and day else None,
        "shipment_date": day.isoformat() if day else None,
        "no_expected_date": not expected,
        "total": total,
        "branch_id": branch["id"] if branch else None,
        "branch_name": _s(merged.get("branch_name")) or (branch["name"] if branch else None),
        "to_warehouse_name": dest_name or None,
        "destination_warehouse_key": side,
        "shipping_address": pickup,
        "delivery_address": dict(_ADDRESS.get(side or "", {"company_name": dest_name})) if dest_name or side else {},
        "notes": _s(merged.get("notes")) or None,
        "last_modified_time": _s(merged.get("last_modified_time")) or None,
        "line_items": _lines(detail, dest_name),
        "lines_loaded": detail is not None,
        "vendor_contact": vendor_person(merged),
        "vendor_address_missing": not bool(pickup) or not bool(vendor_person(merged)["person"] and vendor_person(merged)["phone"]),
        "changed_after_cutoff": pickup_rules.changed_after_cutoff(merged),
        "pickup_state": pickup_state(merged),
    }
    row = SalesOrderCache(
        id=row_id(pid), salesorder_number=number, reference_number=raw["reference_number"], customer_name=vendor, order_status=raw["status"],
        order_date=po_date, expected_shipment_date=day, total=total, billing_address={}, shipping_address=pickup, raw_json=raw,
        synced_at=datetime.now(timezone.utc), assignment_status="unassigned",
    )
    row.order_type = "PO"
    row.source_warehouse = vendor  # the pickup point (stored in sales_orders.source_warehouse for a PO - docs/DATABASE.md)
    row.destination_warehouse = dest_name or None
    return row


def list_rows(start: date, end: date, *, statuses: set[str] | None = None, force: bool = False) -> tuple[list[SalesOrderCache], datetime, int, dict[str, int]]:
    """(rows planned in [start, end] that pass the status filter, when the list was last pulled, orders whose lines are still loading,
    status -> count of every PO- order in [start, end] before eligibility filtering - diagnostic counts only)."""
    records, refreshed = get_records(force=force)
    in_range = [r for r in records.values() if (d := delivery_day(r)[0]) is not None and start <= d <= end]
    counts: dict[str, int] = {}
    for record in in_range:
        status = normalize_status(record.get("status"))
        counts[status] = counts.get(status, 0) + 1
    # Method/creation metadata may be absent on list rows: fill once in the background and fail closed until ready.
    candidates = [r for r in in_range if pickup_rules.classify(r) != "excluded"]
    pending_candidates = request_details(candidates)
    merged = [{**r, **(cached_detail(_record_id(r), r) or {})} for r in candidates]
    shown = [r for r in merged if pickup_state(r) == "scheduled"]
    shown.sort(key=lambda r: ((delivery_day(r)[0] or date.min).isoformat(), _number(r)))
    pending = pending_candidates
    rows = [build_row(r, cached_detail(_record_id(r), r)) for r in shown]
    return rows, refreshed, pending, counts


class NotAssignable(Exception):
    pass


def ensure_row(order_id: str, *, validate: bool = False, enrich_vendor: bool = True) -> SalesOrderCache:
    """Row with its lines for an id, fetching the detail from Zoho (one call) when it is not in memory. `validate` (assigning a fresh PO)
    re-checks the PO- / not-SariSuki rule; an already-assigned PO is loaded without it."""
    pid = zoho_id(order_id)
    with _lock:
        record = (_list["records"] or {}).get(pid)
    detail = cached_detail(pid, record)
    if detail is None:
        detail = fetch_detail(pid, record, enrich_vendor=enrich_vendor)
    base = {**(record or {}), **detail}
    if validate and (not is_wanted(base) or pickup_state(base) != "scheduled"):
        raise NotAssignable(f"{_number(base) or pid} is not a purchase order Load Planning handles (only tomorrow, For Pickup, Issued, pre-cutoff or manager-approved; never SariSuki).")
    return build_row(record or detail, detail)


# ---- API summary -------------------------------------------------------------------------------------------------


def _num(item, key) -> float:
    try:
        return float(item.get(key)) if item.get(key) is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def line_weights(row, *, allow_fetch: bool = False) -> list[float | None]:
    raw = row.raw_json or {}
    return [calculate_line_weight_kg(item.get("quantity"), item.get("unit"), item.get("item_id"), item=item, context=f"PO={row.salesorder_number}", allow_fetch=allow_fetch) for item in raw.get("line_items") or []]


def total_weight_kg(rows) -> tuple[float, bool, list[str]]:
    """(total kg, complete?, item ids whose package weight is still unknown) - the same 'kg+' rule as SO / TO. Cache-only."""
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
    """The list / Confirmed JSON for a purchase order: the sales-order summary keys plus the PO fields."""
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
    branch = branches.describe(raw.get("branch_id"), raw.get("branch_name"))
    result.update(
        order_type="PO",
        purchaseorder_number=raw.get("purchaseorder_number") or row.salesorder_number,
        vendor_name=raw.get("vendor_name") or row.customer_name,
        po_date=raw.get("date"),
        expected_delivery_date=raw.get("delivery_date"),
        no_expected_date=bool(raw.get("no_expected_date")),
        order_status=raw.get("status") or row.order_status,
        status_label=status_label(raw.get("status") or row.order_status),
        source_warehouse=None,
        pickup_from=raw.get("vendor_name") or row.customer_name,
        destination_warehouse=raw.get("to_warehouse_name") or getattr(row, "destination_warehouse", None),
        destination_warehouse_key=raw.get("destination_warehouse_key"),
        driver_ids=list(getattr(row, "helper_ids", None) or ([row.driver_id] if getattr(row, "driver_id", None) else [])),
        assignment_batch_id=getattr(row, "assignment_batch_id", None),
        email_status=getattr(row, "email_status", None),
        email_error=getattr(row, "email_error", None),
        shipping_address=address,
        shipping_city=(address or {}).get("city"),
        zoho_lock=None,
        branch_id=branch["id"] if branch else None,
        branch_name=branch["name"] if branch else raw.get("branch_name"),
        branch_code=branch["code"] if branch else None,
        notes=raw.get("notes"),
        vendor_contact=raw.get("vendor_contact"),
        vendor_address_missing=raw.get("vendor_address_missing"),
        changed_after_cutoff=raw.get("changed_after_cutoff"),
        pickup_state=raw.get("pickup_state"),
        mets_qty_available_for_sale=None,
        glacier_qty_available_for_sale=None,
    )
    sent_at = getattr(row, "email_sent_at", None)
    result["email_sent_at"] = sent_at.isoformat() if hasattr(sent_at, "isoformat") else sent_at
    order_fields.apply(result, row)  # a PO has a standard delivery_method only when Zoho sends one; fulfillment type normally None
    products, packs, cases, units = [], 0.0, 0.0, 0.0
    lines = raw.get("line_items") or []
    for item, weight in zip(lines, line_weights(row, allow_fetch=allow_fetch)):
        quantity = _num(item, "quantity")
        unit = _s(item.get("unit"))
        normalized = unit.casefold()
        is_case = "case" in normalized or "carton" in normalized
        if is_pack_unit(normalized):
            packs += quantity
        elif is_case:
            cases += quantity
        else:
            units += quantity
        products.append({"mets_qty_available_for_sale": None, "glacier_qty_available_for_sale": None, "line_item_id": item.get("line_item_id"), "item_id": item.get("item_id"), "name": item.get("name"), "sku": item.get("sku") or item.get("item_id"), "quantity": quantity, "unit": unit or None, "total_weight_kg": weight, "packaging_type": "pack" if is_pack_unit(normalized) else "case" if is_case else None, "pack_quantity": quantity if is_pack_unit(normalized) else 0, "case_quantity": quantity if is_case else 0, "quantity_packed": 0.0, "quantity_shipped": 0.0})
    result["product_count"] = len([p for p in products if p["name"]])
    result["products"] = products
    result["pack_count"] = packs
    result["case_count"] = cases
    result["unit_count"] = units
    result["total_item_quantity"] = sum(_num(item, "quantity") for item in lines)
    result["lines_loaded"] = bool(raw.get("lines_loaded"))
    return result
