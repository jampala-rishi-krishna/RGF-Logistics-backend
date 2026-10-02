from __future__ import annotations

import logging
import threading
import time
import contextvars
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

from models.inventory import SalesOrderCache
from services.zoho_client import ZohoError, fetch_sales_order_detail, fetch_sales_orders, fetch_sales_orders_by_shipment_date
from services import zoho_acquisition

logger = logging.getLogger("live_sales_order_cache")

WINDOW_TTL_SECONDS = 300  # 5 min

# --- Assignment state: the ONLY thing that's actually persisted for "current" SOs. Loaded
# once from sales_order_history at startup, updated in memory on every assignment mutation,
# never re-read from Neon on a normal request. ---
_state_lock = threading.Lock()
_assignment_state: dict[str, dict] = {}
# Zoho business data (customer, weight, raw_json) for every id currently in _assignment_state,
# fetched live from Zoho (not Neon) and kept warm regardless of which date window is cached.
_assigned_zoho: dict[str, SalesOrderCache] = {}

# --- Ordinary Inventory/unassigned windows: short-TTL cache of whatever Zoho returned for a
# given (start, end) date range. ---
_window_lock = threading.Lock()
_windows: dict[tuple[str, str], tuple[float, dict[str, SalesOrderCache]]] = {}
_windows_unhydrated: dict[tuple[str, str], tuple[float, dict[str, SalesOrderCache]]] = {}

# Per-order Zoho detail, keyed by the list record's last_modified_time: a window re-pull only
# re-fetches the orders Zoho says changed. Memory only, bounded.
_detail_lock = threading.Lock()
_details: dict[str, tuple[str, dict]] = {}
DETAIL_CACHE_MAX = 1500


def _pick(record: dict, *keys: str):
    for key in keys:
        if record.get(key) is not None:
            return record[key]
    return None


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _normalized_order_status(record: dict) -> str | None:
    status = _pick(record, "status", "order_status", "salesorder_status", "sales_order_status", "acknowledgement_status", "acknowledgment_status")
    if isinstance(status, dict):
        status = _pick(status, "status", "name", "label")
    if isinstance(status, str) and status.strip():
        return status.strip().lower().replace("_", " ")
    acknowledged = _pick(record, "acknowledged", "is_acknowledged", "acknowledgement", "acknowledgment")
    if acknowledged is True or str(acknowledged).strip().lower() in {"true", "yes", "1", "acknowledged"}:
        return "acknowledged"
    return None


def record_payload(record: dict) -> dict:
    customer = record.get("customer_name") or (record.get("customer") or {}).get("customer_name") or (record.get("customer") or {}).get("display_name")
    return {
        "id": str(_pick(record, "salesorder_id", "sales_order_id", "id")),
        "salesorder_number": _pick(record, "salesorder_number", "sales_order_number"),
        "reference_number": record.get("reference_number"),
        "customer_name": customer,
        "order_status": _normalized_order_status(record),
        "invoice_status": _pick(record, "invoice_status", "invoiced_status"),
        "payment_status": _pick(record, "payment_status", "paid_status"),
        "shipment_status": _pick(record, "shipment_status", "shipped_status", "shipping_status"),
        "order_date": _as_date(record.get("date") or record.get("order_date")),
        "expected_shipment_date": _as_date(record.get("shipment_date") or record.get("expected_shipment_date")),
        "total": _pick(record, "total", "total_amount"),
        "delivery_method": record.get("delivery_method"),
        "salesperson_name": (record.get("salesperson_name") or (record.get("salesperson") or {}).get("name")),
        "customer_po_number": record.get("customer_po_number"),
        "billing_address": record.get("billing_address") or {},
        "shipping_address": record.get("shipping_address") or {},
        "payment_terms_label": record.get("payment_terms_label"),
        "mode_of_transport": record.get("mode_of_transport"),
        "raw_json": record,
        "synced_at": datetime.now(timezone.utc),
        "assignment_status": "unassigned",
    }


def _build_transient(record: dict) -> SalesOrderCache:
    """A real SalesOrderCache instance, but never added to a DB session - used purely as a
    typed in-memory shape so row_to_dict()/_summary() work unchanged. Never persisted."""
    return SalesOrderCache(**record_payload(record))


def merge_zoho_payload(row: SalesOrderCache, record: dict) -> SalesOrderCache:
    """Merge a fresh Zoho detail response into an in-memory row, including mirrored fields.

    record_payload() always shapes its output as a brand-new, unassigned record - it
    hardcodes assignment_status="unassigned" because that's the correct default when
    building a row from scratch. But when merging fresh Zoho data into an ALREADY
    assigned row (the only case this function is used for - see fleet.py's
    /vehicles/refresh), blindly applying that field back onto the row would silently
    revert a real assignment to "unassigned" while leaving vehicle_id/assigned_at
    untouched, corrupting the row into an inconsistent state and making a still-assigned
    truck's SO vanish from Fleet/Orders on the next periodic Zoho sync. Assignment state
    must only ever change via set_assignment()/_apply_assignment(), never be inferred
    from a plain Zoho detail refresh.
    """
    previous = row.raw_json if isinstance(row.raw_json, dict) else {}
    merged = {**previous, **{key: value for key, value in record.items() if value not in (None, "", [], {})}}
    for key, value in record_payload(merged).items():
        if key in ("id", "assignment_status") or value is None:
            continue
        setattr(row, key, value)
    row.raw_json = merged
    return row


def _apply_assignment(row: SalesOrderCache) -> SalesOrderCache:
    with _state_lock:
        state = _assignment_state.get(row.id)
    if state:
        for key, value in state.items():
            setattr(row, key, value)
    if str(getattr(row, "order_status", "") or "").strip().lower() == "void" and getattr(row, "assignment_status", None) in {"assigned", "manifested", "completed"}:
        row.assignment_status = "released"
        row.release_reason = "Zoho order is void"
        with _state_lock:
            if row.id in _assignment_state:
                _assignment_state[row.id]["assignment_status"] = "released"
                _assignment_state[row.id]["release_reason"] = "Zoho order is void"
    return row


# --- Assignment state: load once at startup, update in memory on every mutation ---


def load_assignment_state_from_history(db) -> None:
    from sqlalchemy import select

    from models.sales_order_history import SalesOrderHistory

    rows = db.execute(select(SalesOrderHistory)).scalars().all()
    with _state_lock:
        _assignment_state.clear()
        for r in rows:
            _assignment_state[r.id] = {
                "vehicle_id": r.vehicle_id,
                "driver_id": r.driver_id,
                "assignment_status": r.assignment_status,
                "assigned_at": r.assigned_at,
                "assigned_by": r.assigned_by,
                "completed_at": r.completed_at,
                "route_id": r.route_id,
                "manifest_id": r.manifest_id,
                "helper_ids": r.helper_ids or [],
            }
    logger.info("[LiveSalesOrderCache] Loaded assignment state for %d SOs from sales_order_history", len(_assignment_state))


def set_assignment(order_id: str, **fields) -> None:
    with _state_lock:
        _assignment_state.setdefault(order_id, {}).update(fields)
    record = _assigned_zoho.get(order_id)
    if record is not None:
        for key, value in fields.items():
            setattr(record, key, value)


def get_assignment(order_id: str) -> dict | None:
    with _state_lock:
        return dict(_assignment_state[order_id]) if order_id in _assignment_state else None


# --- Zoho data for currently-assigned SOs (any date), refreshed live, never from Neon ---


def ensure_zoho_data(order_id: str) -> SalesOrderCache | None:
    """Fetch this SO's Zoho detail if we don't already have it cached. Used for capacity
    checks and Confirmed SO's current/future view, which need the assigned set regardless
    of what date window Inventory happened to have cached."""
    epoch = zoho_acquisition.generation()
    existing = _assigned_zoho.get(order_id)
    if existing is not None:
        return existing
    try:
        detail = fetch_sales_order_detail(order_id)
    except ZohoError:
        return None
    return publish_zoho_data(order_id, detail, epoch)


def publish_zoho_data(order_id: str, detail: dict, epoch: int) -> SalesOrderCache:
    """Publish an already acquired full detail using the existing snapshot shaping."""
    record = deepcopy(detail.get("salesorder") or detail)
    row = _build_transient(record)
    with zoho_acquisition.publication(epoch) as current:
        if current:
            _apply_assignment(row)
            _assigned_zoho[order_id] = row
    return row


def refresh_zoho_data(order_id: str) -> SalesOrderCache | None:
    with zoho_acquisition.invalidation():
        _assigned_zoho.pop(order_id, None)
    return ensure_zoho_data(order_id)


def get_assigned_snapshot() -> list[SalesOrderCache]:
    """All SOs currently in an active assignment state (assigned/manifested/completed),
    with fresh-as-cached Zoho data. This is what capacity checks, Confirmed SO's
    current/future view, and Fleet's associated_sos all read - zero Neon queries."""
    rows, _ = get_assigned_snapshot_ex()
    return rows


def get_assigned_snapshot_ex() -> tuple[list[SalesOrderCache], bool]:
    """Same set as get_assigned_snapshot(), but also reports whether any assigned SO's
    Zoho detail fetch failed this call (had_failures). Right after a backend restart,
    _assigned_zoho is empty, so every currently-assigned SO needs fetching at once - a
    single transient Zoho hiccup used to make that SO silently vanish from the snapshot,
    and Fleet's rebuild-and-latch cache (fleet.py) would then cache that gap until the
    next manual refresh, making a still-assigned truck's SO/load/fulfillment columns show
    "-" indefinitely. Callers that cache their result (Fleet) should check had_failures
    and avoid latching so the next request retries instead of staying stuck."""
    with _state_lock:
        ids = [oid for oid, s in _assignment_state.items() if (s.get("assignment_status") or "unassigned") != "unassigned"]
    to_fetch = [oid for oid in ids if oid not in _assigned_zoho]
    had_failures = False
    if to_fetch:
        # Fan out like _hydrate_details, bounded well under Zoho's ~100 req/min ceiling,
        # so a cold cache with many assigned SOs doesn't fetch them one at a time.
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(contextvars.copy_context().run, ensure_zoho_data, oid): oid for oid in to_fetch}
            for future in as_completed(futures):
                if future.result() is None:
                    had_failures = True
    rows = []
    for order_id in ids:
        row = _assigned_zoho.get(order_id)
        if row is not None:
            rows.append(row)
        else:
            had_failures = True
    return rows, had_failures


# --- General Inventory/unassigned windows: short-TTL live Zoho cache ---


def _shipment_filter_applied(page_context: dict) -> bool:
    return any(isinstance(c, dict) and c.get("column_name") == "shipment_date" for c in (page_context.get("search_criteria") or []))


def _pull_window(start: date | None, end: date | None) -> dict[str, dict]:
    """List the orders shipping in [start, end]. Zoho filters by shipment date server-side
    (1 page for a typical day) instead of scanning 90 days of orders by order date. If a
    tenant ever stops honoring that filter, fall back to the legacy scan so results stay
    correct."""
    if start is None or end is None:
        return _pull_window_legacy(start, end)
    records: dict[str, dict] = {}
    page = 1
    seen_fingerprints: set[tuple] = set()
    while True:
        payload = fetch_sales_orders_by_shipment_date(start, end, page=page, per_page=200)
        context = payload.get("page_context") or {}
        if page == 1 and not _shipment_filter_applied(context):
            logger.warning("[LiveSalesOrderCache] shipment-date filter not applied by Zoho - using legacy window scan")
            return _pull_window_legacy(start, end)
        page_records = payload.get("salesorders") or []
        fingerprint = tuple(str(r.get("salesorder_id") or r.get("id") or "") for r in page_records)
        if fingerprint and fingerprint in seen_fingerprints:
            break
        if fingerprint:
            seen_fingerprints.add(fingerprint)
        for record in page_records:
            record_id = str(record.get("salesorder_id") or record.get("id") or "")
            expected_shipment = _as_date(record.get("shipment_date") or record.get("expected_shipment_date"))
            if record_id and expected_shipment and start <= expected_shipment <= end:
                records[record_id] = record
        has_more = context.get("has_more_page")
        if isinstance(has_more, str):
            has_more = has_more.strip().lower() == "true"
        if not page_records or not has_more:
            break
        page += 1
    return records


def _pull_window_legacy(start: date | None, end: date | None) -> dict[str, dict]:
    records: dict[str, dict] = {}
    page = 1
    seen_fingerprints: set[tuple] = set()
    from datetime import timedelta

    lookup_start = start - timedelta(days=90) if start else None
    while True:
        payload = fetch_sales_orders(date_from=lookup_start, date_to=end, page=page, per_page=200)
        page_records = payload.get("salesorders") or []
        fingerprint = tuple(str(r.get("salesorder_id") or r.get("id") or "") for r in page_records)
        if fingerprint and fingerprint in seen_fingerprints:
            break
        if fingerprint:
            seen_fingerprints.add(fingerprint)
        for record in page_records:
            record_id = str(record.get("salesorder_id") or record.get("id") or "")
            expected_shipment = _as_date(record.get("shipment_date") or record.get("expected_shipment_date"))
            if record_id and expected_shipment and (start is None or start <= expected_shipment <= end):
                records[record_id] = record
        context = payload.get("page_context") or {}
        has_more = context.get("has_more_page")
        if isinstance(has_more, str):
            has_more = has_more.strip().lower() == "true"
        if not page_records or (not has_more and len(page_records) < 200):
            break
        page += 1
    return records


def _remember_detail(rid: str, modified: str, fields: dict) -> None:
    with _detail_lock:
        _details.pop(rid, None)
        _details[rid] = (modified, fields)
        while len(_details) > DETAIL_CACHE_MAX:
            _details.pop(next(iter(_details)), None)


def _hydrate_details(records: dict[str, dict]) -> None:
    # Reuse cached detail for every order whose last_modified_time is unchanged; fetch only
    # the rest. Keep fetching below Zoho's approximate 100 requests/minute ceiling.
    to_fetch: list[str] = []
    for rid, record in records.items():
        modified = str(record.get("last_modified_time") or "")
        with _detail_lock:
            hit = _details.get(rid)
        if modified and hit is not None and hit[0] == modified:
            records[rid] = {**record, **hit[1]}
        else:
            to_fetch.append(rid)
    if not to_fetch:
        return
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(contextvars.copy_context().run, fetch_sales_order_detail, rid): rid for rid in to_fetch}
        for future in as_completed(futures):
            rid = futures[future]
            try:
                detail = future.result()
                full = detail.get("salesorder") or detail
                fields = {k: v for k, v in full.items() if v not in (None, "", [], {})}
                modified = str(records[rid].get("last_modified_time") or "")
                records[rid] = {**records[rid], **fields}
                if modified:
                    _remember_detail(rid, modified, fields)
            except ZohoError:
                continue


def get_window(start: date, end: date, *, hydrate: bool = True) -> list[SalesOrderCache]:
    """hydrate=False skips the per-order Zoho detail fetch (line_items etc.) - only use that
    for callers that just need list-level fields (e.g. the city filter), never for anything
    that renders per-line weight/pack breakdowns. Cached separately from the hydrated window
    (same TTL) so it doesn't defeat caching for callers that don't need hydration."""
    epoch = zoho_acquisition.generation()
    key = (start.isoformat(), end.isoformat())
    cache = _windows if hydrate else _windows_unhydrated
    with _window_lock:
        entry = cache.get(key)
        if entry is not None and time.monotonic() - entry[0] <= WINDOW_TTL_SECONDS:
            return [_apply_assignment(row) for row in entry[1].values()]
    raw = _pull_window(start, end)
    if hydrate:
        _hydrate_details(raw)
    rows = {rid: _build_transient(record) for rid, record in raw.items()}
    with zoho_acquisition.publication(epoch) as current:
        if current:
            with _window_lock:
                cache[key] = (time.monotonic(), rows)
    return [_apply_assignment(row) for row in rows.values()]


def invalidate_assigned_zoho() -> None:
    """Force every currently-assigned SO's cached Zoho data to be re-fetched on next use."""
    with zoho_acquisition.invalidation():
        _assigned_zoho.clear()


def invalidate_windows() -> None:
    """Call on the Refresh button / after a Zoho-side change - forces the next read to
    re-pull from Zoho instead of serving the TTL cache."""
    with zoho_acquisition.invalidation():
        with _window_lock:
            _windows.clear()
            _windows_unhydrated.clear()


def mark_acknowledged(order_id: str, acknowledged: bool) -> None:
    """Reflect an acknowledge / remove-acknowledge that already succeeded in Zoho on every
    cached copy of the order, so the next list read is served from cache instead of
    re-pulling and re-hydrating the whole window. Zoho tracks this as the order's
    sub-status (status itself stays "confirmed")."""
    sub_status = "cs_acknowl" if acknowledged else "confirmed"

    def patch(row) -> None:
        raw = row.raw_json if isinstance(row.raw_json, dict) else {}
        row.raw_json = {**raw, "current_sub_status": sub_status, "order_sub_status": sub_status}

    with _window_lock:
        for cache in (_windows, _windows_unhydrated):
            for _, rows in cache.values():
                if order_id in rows:
                    patch(rows[order_id])
    assigned = _assigned_zoho.get(order_id)
    if assigned is not None:
        patch(assigned)
    with _detail_lock:
        _details.pop(order_id, None)


def prewarm_default_windows() -> None:
    """Startup warm-up (background thread): load today's and tomorrow's windows so the first
    Load Planning open after a deploy/restart is served from memory. Best effort."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Manila")).date()
    for day in (today + timedelta(days=1), today):
        try:
            get_window(day, day)
            logger.info("[LiveSalesOrderCache] prewarmed window %s", day)
        except Exception as exc:  # never let a warm-up failure affect startup
            logger.warning("[LiveSalesOrderCache] prewarm %s failed: %s", day, exc)


def get_current_orders(days_ahead: int = 30) -> list[SalesOrderCache]:
    """Merges a default live-Zoho window with the full assigned snapshot (which may include
    orders shipping outside that window) - used by dashboards/plans that don't take an
    explicit date range. Zero Neon reads."""
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    window = get_window(today, today + timedelta(days=days_ahead))
    merged = {row.id: row for row in window}
    for row in get_assigned_snapshot():
        merged[row.id] = row
    return list(merged.values())


def find_cached(order_id: str) -> SalesOrderCache | None:
    """Best-effort lookup across whatever's currently cached (assigned snapshot + any
    cached window) - used for acknowledge/detail endpoints. Falls back to a live Zoho
    fetch (ensure_zoho_data) when not found, since Zoho is the source of truth."""
    row = _assigned_zoho.get(order_id)
    if row is not None:
        return row
    with _window_lock:
        for _, records in _windows.values():
            if order_id in records:
                return records[order_id]
    return None
