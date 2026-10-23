from __future__ import annotations

import logging
import os
import re
import threading
import time
import contextvars
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from models.inventory import SalesOrderCache
from services.zoho_client import ZohoError, fetch_sales_order_detail, fetch_sales_orders, fetch_sales_orders_by_shipment_date
from services import branches, memory_debug, order_types, purchase_orders, text_repair, transfer_orders, zoho_acquisition, zoho_resilience, zoho_usage

logger = logging.getLogger("live_sales_order_cache")

WINDOW_TTL_SECONDS = 300  # 5 min
PHT = ZoneInfo("Asia/Manila")
WIDE_WINDOW_PAST_DAYS = int(os.environ.get("ZOHO_WINDOW_PAST_DAYS", "1"))
WIDE_WINDOW_FUTURE_DAYS = int(os.environ.get("ZOHO_WINDOW_FUTURE_DAYS", "14"))
FULL_REPULL_SECONDS = int(os.environ.get("ZOHO_FULL_REPULL_SECONDS", "7200"))
RECONCILE_SECONDS = int(os.environ.get("ZOHO_RECONCILE_SECONDS", "7200"))

# --- Assignment state: the ONLY thing that's actually persisted for "current" SOs. Loaded
# once from sales_order_history at startup, updated in memory on every assignment mutation,
# never re-read from Neon on a normal request. ---
_state_lock = threading.Lock()
_assignment_state: dict[str, dict] = {}
# Bumped on every assignment-state mutation (assign, unassign, email status, completion...). The driver app's
# in-memory "today" cache compares it, so it re-reads Neon only after something actually changed.
_assignment_generation = 0


def assignment_generation() -> int:
    return _assignment_generation


def bump_assignment_generation() -> None:
    """Call after the Neon commit of an assignment change, so a reader that raced the commit re-reads."""
    global _assignment_generation
    with _state_lock:
        _assignment_generation += 1
ASSIGNED_ZOHO_MAX = int(os.environ.get("ZOHO_ASSIGNED_CACHE_MAX", "500"))  # rows of orders NOT in an active assignment
ASSIGNED_ZOHO_TTL_SECONDS = int(os.environ.get("ZOHO_ASSIGNED_CACHE_TTL_SECONDS", "1800"))


class _OrderRowCache(dict):
    """order id -> row. Rows of orders in an active assignment (any status but unassigned) are never evicted: they are the working set
    Fleet / capacity / Confirmed read, and re-fetching them would cost Zoho calls. Every other row (a drawer opened on an unassigned
    order, an order that was unassigned again) is dropped after ASSIGNED_ZOHO_TTL_SECONDS, and the oldest go first once there are more
    than ASSIGNED_ZOHO_MAX of them. Memory only; pruning runs on insert, so a read never pays for it."""

    def __init__(self):
        super().__init__()
        self._at: dict[str, float] = {}
        self._guard = threading.RLock()

    def __setitem__(self, key, value):
        with self._guard:
            super().pop(key, None)  # re-insert at the end: insertion order is age order
            super().__setitem__(key, value)
            self._at[key] = time.monotonic()
            self._prune()

    def __delitem__(self, key):
        with self._guard:
            super().__delitem__(key)
            self._at.pop(key, None)

    def pop(self, key, *default):
        with self._guard:
            self._at.pop(key, None)
            return super().pop(key, *default)

    def clear(self):
        with self._guard:
            super().clear()
            self._at.clear()

    def _prune(self) -> None:
        now = time.monotonic()
        with _state_lock:
            active = {oid for oid in self if (_assignment_state.get(oid) or {}).get("assignment_status") not in (None, "unassigned")}
        evictable = [oid for oid in self if oid not in active]  # oldest first
        expired = [oid for oid in evictable if now - self._at.get(oid, now) >= ASSIGNED_ZOHO_TTL_SECONDS]
        overflow = max(0, len(evictable) - len(expired) - ASSIGNED_ZOHO_MAX)
        expired_set = set(expired)
        remaining = [oid for oid in evictable if oid not in expired_set]
        for oid in expired + remaining[:overflow]:
            super().pop(oid, None)
            self._at.pop(oid, None)


# Zoho business data (customer, weight, raw_json) for every id currently in _assignment_state,
# fetched live from Zoho (not Neon) and kept warm regardless of which date window is cached.
# Also holds orders whose drawer was opened (publish_zoho_data); those are bounded, see _OrderRowCache.
_assigned_zoho: dict[str, SalesOrderCache] = _OrderRowCache()
# Ids (-> number) of orders found to belong to a branch RareChain does not handle. Memory only.
_excluded_ids: dict[str, str | None] = {}

# --- Ordinary Inventory/unassigned windows: cache of whatever Zoho returned for a given (start, end)
# date range. The current wide window (today-1 .. today+14) is the shared one: it is served from memory
# and kept fresh by refresh_shared_window(), never evicted. Any other range (a past date, a far-future
# range, yesterday's wide window after midnight) is dropped once nobody has read it for
# WINDOW_TTL_SECONDS, and at most WINDOW_CACHE_MAX ranges are held (least recently used dropped first).
# Each entry is (last read, rows). ---
WINDOW_CACHE_MAX = int(os.environ.get("ZOHO_WINDOW_CACHE_MAX", "4"))
_window_lock = threading.Lock()
_windows: dict[tuple[str, str], tuple[float, dict[str, SalesOrderCache]]] = {}
_windows_unhydrated: dict[tuple[str, str], tuple[float, dict[str, SalesOrderCache]]] = {}

# Per-order Zoho detail, keyed by the list record's last_modified_time: a window re-pull only
# re-fetches the orders Zoho says changed. Memory only, bounded.
_detail_lock = threading.Lock()
_details: dict[str, tuple[str, dict]] = {}
_webhook_copies: dict[str, tuple[str, dict]] = {}
_hybrid_details: dict[tuple[str, str], dict] = {}
DETAIL_CACHE_MAX = 1500
_wide_window_key: tuple[str, str] | None = None
_wide_window_last_sync: datetime | None = None
_wide_window_last_full: float = 0.0
_outside_window_cache: dict[tuple[str, str], tuple[float, dict[str, SalesOrderCache]]] = {}


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


_ON_HOLD_SUB_STATUSES = {"csonhold", "onhold"}


def _squash(value) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def is_acknowledged_sub_status(value) -> bool:
    return _squash(value) == "csacknowl"


def record_has_acknowledged_sub_status(record: dict | None) -> bool:
    if not isinstance(record, dict):
        return False
    return any(is_acknowledged_sub_status(record.get(key)) for key in ("current_sub_status", "order_sub_status"))


def is_on_hold(row_or_record) -> bool:
    """True for a Sales Order Zoho has put On Hold. Zoho models it as the built-in sub-status
    `cs_onhold` (current_sub_status / order_sub_status, status stays "confirmed"). Matching is
    case/spacing-insensitive. Accepts a Zoho record dict or a row carrying one in raw_json."""
    record = row_or_record if isinstance(row_or_record, dict) else (getattr(row_or_record, "raw_json", None) or {})
    if not isinstance(record, dict):
        return False
    sub_statuses = (record.get("current_sub_status"), record.get("order_sub_status"))
    if any(_squash(value) in _ON_HOLD_SUB_STATUSES for value in sub_statuses):
        return True
    # Every hold sub-status (cs_onhold = Accounting, cs_onholds = "ON HOLD(SALES)", any future cs_onhold*).
    if any(_squash(value).startswith("csonhold") for value in sub_statuses):
        return True
    # Detail payloads list the org's sub-statuses with display names; resolve this order's own by id/code.
    current_id = str(record.get("current_sub_status_id") or record.get("order_sub_status_id") or "")
    current_code = _squash(record.get("current_sub_status") or record.get("order_sub_status"))
    for sub in record.get("sub_statuses") or []:
        if not isinstance(sub, dict):
            continue
        if (current_id and str(sub.get("status_id")) == current_id) or (current_code and _squash(sub.get("status_code")) == current_code):
            if "hold" in _squash(sub.get("display_name")):
                return True
    return _squash(_normalized_order_status(record) or getattr(row_or_record, "order_status", "")) == "onhold"


# Zoho sometimes hands back text whose UTF-8 bytes were decoded as Windows-1252; undone at ingestion (services/text_repair.py).
_MOJIBAKE = text_repair._MOJIBAKE
repair_text = text_repair.repair_text
repair_deep = text_repair.repair_deep


def record_payload(record: dict) -> dict:
    record = repair_deep(record)
    custom = record.get("custom_field_hash") if isinstance(record.get("custom_field_hash"), dict) else {}
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
        "customer_po_number": record.get("customer_po_number") or custom.get("cf_customer_po_number") or custom.get("cf_customer_po_number_formatted"),
        "billing_address": record.get("billing_address") or {},
        "shipping_address": record.get("shipping_address") or {},
        "payment_terms_label": record.get("payment_terms_label"),
        "mode_of_transport": record.get("mode_of_transport") or custom.get("cf_mode_of_transportation_for_") or custom.get("cf_mode_of_transportation_for__formatted"),
        "raw_json": record,
        "synced_at": datetime.now(timezone.utc),
        "assignment_status": "unassigned",
    }


def _build_transient(record: dict) -> SalesOrderCache:
    """A real SalesOrderCache instance, but never added to a DB session - used purely as a
    typed in-memory shape so row_to_dict()/_summary() work unchanged. Never persisted."""
    row = SalesOrderCache(**record_payload(record))
    if order_types.is_wm_number(row.salesorder_number):
        row.order_type = "WM"  # wet market: same Zoho sales-order list, split by number prefix only
    return row


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
                "assigned_by_name": getattr(r, "assigned_by_name", None),
                "completed_at": r.completed_at,
                "route_id": r.route_id,
                "manifest_id": r.manifest_id,
                "helper_ids": r.helper_ids or [],
                "email_status": r.email_status,
                "email_error": r.email_error,
                "email_sent_at": r.email_sent_at,
                "email_message_id": r.email_message_id,
                "assignment_batch_id": r.assignment_batch_id,
                "expected_shipment_date": r.expected_shipment_date,
            }
    by_status: dict[str, int] = {}
    for state in _assignment_state.values():
        key = state.get("assignment_status") or "unassigned"
        by_status[key] = by_status.get(key, 0) + 1
    logger.info("[LiveSalesOrderCache] Loaded assignment state for %d SOs from sales_order_history (by status: %s)",
                len(_assignment_state), dict(sorted(by_status.items())))


def set_assignment(order_id: str, **fields) -> None:
    global _assignment_generation
    with _state_lock:
        _assignment_state.setdefault(order_id, {}).update(fields)
        _assignment_generation += 1
    record = _assigned_zoho.get(order_id)
    if record is not None:
        for key, value in fields.items():
            setattr(record, key, value)


def ids_for_batch(batch_id: str) -> list[str]:
    """Sales orders of ONE assignment (same assignment_batch_id), still assigned. Memory only."""
    with _state_lock:
        return [oid for oid, state in _assignment_state.items()
                if state.get("assignment_batch_id") == batch_id and (state.get("assignment_status") or "unassigned") != "unassigned"]


def local_zoho_warmup_disabled() -> bool:
    """LOCAL_DISABLE_ZOHO_WARMUP=true (default off): for running the backend on a developer machine with minimal
    Zoho traffic. Turns off the startup Fleet warm-up and the scheduled fleet sync, and makes the Fleet build use
    only Zoho data already in memory (so a fresh local process makes ZERO Zoho calls for /vehicles; the map and
    GPS work, but trucks show no associated Sales Orders). Never set it in production."""
    return os.environ.get("LOCAL_DISABLE_ZOHO_WARMUP", "").strip().lower() in {"1", "true", "yes"}


def startup_seed_enabled() -> bool:
    return os.environ.get("ZOHO_STARTUP_SEED", "true").strip().lower() in {"1", "true", "yes"}


def _seed_row(record: dict) -> None:
    record_id = str(record.get("salesorder_id") or record.get("sales_order_id") or record.get("id") or "")
    if not record_id or not branches.is_allowed(record):
        return
    modified = str(record.get("last_modified_time") or "")
    row = _build_transient(record)
    _remember_detail(record_id, modified, record)
    with _detail_lock:
        _webhook_copies[record_id] = (modified, dict(record))
    _observe_dates([record])
    with _window_lock:
        start, end = _wide_bounds()
        key = (start.isoformat(), end.isoformat())
        existing = _windows.get(key)
        rows = dict(existing[1]) if existing else {}
        rows[record_id] = _apply_assignment(row)
        _store_window_locked(_windows, key, rows)
    with _state_lock:
        assigned = (_assignment_state.get(record_id) or {}).get("assignment_status") not in (None, "unassigned")
    if assigned:
        _assigned_zoho[record_id] = _apply_assignment(row)


def startup_seed() -> dict:
    """Seed current SO memory from the separate Neon cache, then walk one Zoho list delta."""
    if not startup_seed_enabled():
        return {"enabled": False, "rows": 0, "neon_bytes": 0, "zoho_pages": 0, "zoho_details": 0}
    from services import zoho_so_live_cache
    today = datetime.now(PHT).date()
    start, end = today - timedelta(days=1), today + timedelta(days=14)
    try:
        rows, approx_bytes = zoho_so_live_cache.load_window(start, end, branches.allowed_branch_ids())
        for record in rows:
            _seed_row(record)
        watermark = zoho_so_live_cache.max_modified(rows) or datetime.min.replace(tzinfo=timezone.utc)
        delta = _pull_delta_since(start, end, watermark)
        _hydrate_details(delta)
        for record in delta.values():
            _seed_row(record)
            zoho_so_live_cache.upsert_snapshot(record, source="api")
        deleted = zoho_so_live_cache.cleanup(today)
        logger.info("[ZOHO_STARTUP_SEED] rows=%d approx_bytes=%d delta_rows=%d deleted=%d", len(rows), approx_bytes, len(delta), deleted)
        return {"enabled": True, "rows": len(rows), "neon_bytes": approx_bytes, "zoho_pages": 1, "zoho_details": len(delta)}
    except Exception as exc:
        logger.warning("[ZOHO_STARTUP_SEED] Neon unavailable; falling back to full hydration: %s", type(exc).__name__)
        refresh_shared_window(force_full=True)
        return {"enabled": True, "rows": 0, "neon_bytes": 0, "zoho_pages": 1, "zoho_details": len(_details), "fallback": True}


def cleanup_live_cache_daily() -> int:
    """Remove expired live snapshots without touching historical order tables."""
    from services import zoho_so_live_cache
    deleted = zoho_so_live_cache.cleanup(datetime.now(PHT).date())
    logger.info("[ZOHO_LIVE_CACHE] daily_cleanup deleted=%d", deleted)
    return deleted


def has_active_assignments() -> bool:
    """True when at least one SO is in the "assigned" state. Memory only, never calls Zoho."""
    with _state_lock:
        return any(s.get("assignment_status") == "assigned" for s in _assignment_state.values())


def cached_assigned_records() -> list:
    """Assigned SOs whose Zoho data is ALREADY in memory. Never calls Zoho (unlike get_assigned_snapshot)."""
    with _state_lock:
        return list(_assigned_zoho.values())


def get_assignment(order_id: str) -> dict | None:
    with _state_lock:
        return dict(_assignment_state[order_id]) if order_id in _assignment_state else None


# --- Zoho data for currently-assigned SOs (any date), refreshed live, never from Neon ---


def ensure_zoho_data(order_id: str) -> SalesOrderCache | None:
    """Fetch this SO's Zoho detail if we don't already have it cached. Used for capacity
    checks and Confirmed SO's current/future view, which need the assigned set regardless
    of what date window Inventory happened to have cached."""
    if transfer_orders.is_to_id(order_id):
        with _state_lock:
            known = order_id in _assignment_state  # an already-assigned TO is loaded as it is; a fresh one must still be a Draft Mets<->Glacier TO
        return _ensure_transfer_order(order_id, validate=not known)
    if purchase_orders.is_po_id(order_id):
        with _state_lock:
            known = order_id in _assignment_state
        return _ensure_purchase_order(order_id, validate=not known)
    if order_id in _excluded_ids:
        raise branches.BranchNotAllowed(_excluded_ids[order_id])  # no Zoho call for a known excluded order
    epoch = zoho_acquisition.generation()
    existing = _assigned_zoho.get(order_id)
    if existing is not None:
        return existing
    try:
        detail = fetch_sales_order_detail(order_id)
    except ZohoError:
        return None
    return publish_zoho_data(order_id, detail, epoch)


def _ensure_transfer_order(order_id: str, *, validate: bool = False) -> SalesOrderCache | None:
    """A transfer order (id "TO:<zoho id>"): lines come from the TO cache, or one Zoho detail call. Held with the assigned rows so
    capacity checks, Fleet and the driver app see it exactly like an SO. `validate` is for assigning: Draft + Mets<->Glacier only."""
    existing = _assigned_zoho.get(order_id)
    if existing is not None:
        return existing
    epoch = zoho_acquisition.generation()
    try:
        row = transfer_orders.ensure_row(order_id, validate=validate)
    except ZohoError:
        return None
    except transfer_orders.NotAssignable as exc:
        raise HTTPException(409, str(exc)) from exc
    with zoho_acquisition.publication(epoch) as current:
        if current:
            _apply_assignment(row)
            _assigned_zoho[order_id] = row
    return _apply_assignment(row)


def _ensure_purchase_order(order_id: str, *, validate: bool = False) -> SalesOrderCache | None:
    """A purchase order (id "PO:<zoho id>"): same as a TO - lines from the PO cache or one Zoho detail call, held with the assigned rows."""
    existing = _assigned_zoho.get(order_id)
    if existing is not None:
        return existing
    epoch = zoho_acquisition.generation()
    try:
        row = purchase_orders.ensure_row(order_id, validate=validate, enrich_vendor=validate)
    except ZohoError:
        return None
    except purchase_orders.NotAssignable as exc:
        raise HTTPException(409, str(exc)) from exc
    with zoho_acquisition.publication(epoch) as current:
        if current:
            _apply_assignment(row)
            _assigned_zoho[order_id] = row
    return _apply_assignment(row)


def purchase_order_rows(start: date, end: date, *, statuses: set[str] | None = None, force: bool = False):
    """PO- purchase orders planned in [start, end] with the assignment state applied: (rows, last refreshed, details still loading,
    status counts). force=True is the Hard refresh button."""
    rows, refreshed, pending, counts = purchase_orders.list_rows(start, end, statuses=statuses, force=force)
    return [_apply_assignment(row) for row in rows], refreshed, pending, counts


def transfer_order_rows(start: date, end: date, *, force: bool = False):
    """Draft Mets<->Glacier TOs dated in [start, end] with the assignment state applied: (rows, last refreshed, details still loading).
    force=True is the Hard refresh button - the only way to bypass the TTL."""
    rows, refreshed, pending = transfer_orders.list_rows(start, end, force=force)
    return [_apply_assignment(row) for row in rows], refreshed, pending


def publish_zoho_data(order_id: str, detail: dict, epoch: int) -> SalesOrderCache:
    """Publish an already acquired full detail using the existing snapshot shaping."""
    record = deepcopy(detail.get("salesorder") or detail)
    _persist_live_snapshot(record, "api")
    if not branches.is_allowed(record):
        _excluded_ids[str(order_id)] = record.get("salesorder_number")
        raise branches.BranchNotAllowed(record.get("salesorder_number"))
    row = _build_transient(record)
    _observe_dates([record])
    with zoho_acquisition.publication(epoch) as current:
        if current:
            _apply_assignment(row)
            _assigned_zoho[order_id] = row
    return row


def _observe_dates(records) -> None:
    """A4: hand the records of this (already made) Zoho sync to the automatic date-change unassign. Memory only, no Zoho call."""
    try:
        from services import auto_unassign

        auto_unassign.observe_zoho_records(list(records))
    except Exception as exc:  # never break a sync
        logger.warning("[LiveSalesOrderCache] date-change check skipped: %s", type(exc).__name__)


def _persist_live_snapshot(record: dict, source: str) -> None:
    """Persist current data in the separate live table; never affect the request on DB failure."""
    try:
        from services import zoho_so_live_cache
        zoho_so_live_cache.upsert_snapshot(record, source=source)
    except Exception as exc:
        logger.warning("[ZOHO_LIVE_CACHE] snapshot write skipped source=%s error=%s", source, type(exc).__name__)


def require_allowed(order_id: str) -> None:
    """Raise BranchNotAllowed (409) for an order already known to belong to a branch we do not
    handle - without calling Zoho. Unknown ids pass; ensure_zoho_data classifies them on fetch."""
    if str(order_id) in _excluded_ids:
        raise branches.BranchNotAllowed(_excluded_ids[str(order_id)])


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


def _ensure_or_skip(order_id: str):
    """ensure_zoho_data for the snapshot fan-out: an order of a branch we do not handle is hidden
    (remembered in _excluded_ids), not counted as a fetch failure."""
    try:
        return ensure_zoho_data(order_id), False
    except branches.BranchNotAllowed:
        return None, True


STALE_ASSIGNED_GRACE_DAYS = 1  # an assigned SO is still hydrated when it shipped yesterday or later (Manila)
_stale_log = {"count": None, "at": 0.0}


def _stale_cutoff(today: date | None = None) -> date:
    return (today or datetime.now(PHT).date()) - timedelta(days=STALE_ASSIGNED_GRACE_DAYS)


def _is_stale_assigned(state: dict, cutoff: date) -> bool:
    """True for an assigned SO whose expected shipment date is before `cutoff`. An unknown date is never called stale."""
    shipped = state.get("expected_shipment_date")
    if isinstance(shipped, datetime):
        shipped = shipped.date()
    return isinstance(shipped, date) and shipped < cutoff


def stale_assigned_count(today: date | None = None) -> int:
    """How many SOs are still in an assigned/manifested/completed state although they shipped before yesterday. Memory only."""
    cutoff = _stale_cutoff(today)
    with _state_lock:
        return sum(1 for s in _assignment_state.values() if (s.get("assignment_status") or "unassigned") not in ("unassigned", "released") and _is_stale_assigned(s, cutoff))


def _log_skipped_stale(count: int, cutoff: date) -> None:
    """One INFO line when the count changes (and at most hourly otherwise), never one per request."""
    now = time.monotonic()
    if count and (_stale_log["count"] != count or now - _stale_log["at"] > 3600):
        logger.info("[LiveSalesOrderCache] skipped %d assigned SO(s) that shipped before %s: no Zoho call is made for them (see docs/DATABASE.md, stale assignments)", count, cutoff.isoformat())
        _stale_log["count"], _stale_log["at"] = count, now


def get_assigned_snapshot_ex(include_window: bool = True, statuses: frozenset[str] | None = None, block: bool = True) -> tuple[list[SalesOrderCache], bool]:
    """Same set as get_assigned_snapshot(), but also reports whether any assigned SO's
    Zoho detail fetch failed this call (had_failures). Right after a backend restart,
    _assigned_zoho is empty, so every currently-assigned SO needs fetching at once - a
    single transient Zoho hiccup used to make that SO silently vanish from the snapshot,
    and Fleet's rebuild-and-latch cache (fleet.py) would then cache that gap until the
    next manual refresh, making a still-assigned truck's SO/load/fulfillment columns show
    "-" indefinitely. Callers that cache their result (Fleet) should check had_failures
    and avoid latching so the next request retries instead of staying stuck.

    include_window=False skips the shared 16-day shipment window and fetches just the assigned SOs
    that are not in memory yet (one detail call each). Fleet uses this: it needs only the assigned
    set, so a cold start costs ~one call per assigned SO instead of a full window re-pull.

    statuses (e.g. frozenset({"assigned"})) limits the set to SOs in those assignment states. Fleet shows only
    "assigned" orders, so it passes that and never pays a Zoho call for completed/released/manifested history."""
    block = block or _blocking_reads_forced()
    cutoff = _stale_cutoff()
    with _state_lock:
        candidates = [(oid, s) for oid, s in _assignment_state.items()
                      if (s.get("assignment_status") or "unassigned") != "unassigned" and oid not in _excluded_ids
                      and (statuses is None or s.get("assignment_status") in statuses)]
    # Old "assigned" rows (the shipment date is long past) must not cost a Zoho detail call at startup or on a Fleet rebuild.
    ids = [oid for oid, s in candidates if not _is_stale_assigned(s, cutoff)]
    _log_skipped_stale(len(candidates) - len(ids), cutoff)
    if ids and include_window:
        start, end = _wide_bounds()
        get_window(start, end, block=block)
        with _window_lock:
            for _, records in _windows.values():
                for oid in ids:
                    row = records.get(oid)
                    if row is not None and oid not in _assigned_zoho:
                        _assigned_zoho[oid] = _apply_assignment(row)
    to_fetch = [oid for oid in ids if oid not in _assigned_zoho]
    had_failures = False
    if to_fetch and not block:
        _start_background_warm(("assigned", include_window, tuple(sorted(statuses or ()))), lambda: get_assigned_snapshot_ex(include_window=include_window, statuses=statuses))
        raise WindowWarming("Assigned sales orders are loading from Zoho. Try again in a few seconds.")
    if to_fetch:
        # Fan out like _hydrate_details, bounded well under Zoho's ~100 req/min ceiling,
        # so a cold cache with many assigned SOs doesn't fetch them one at a time.
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(contextvars.copy_context().run, _ensure_or_skip, oid): oid for oid in to_fetch}
            for future in as_completed(futures):
                row, excluded = future.result()
                if row is None and not excluded:
                    had_failures = True
    rows = []
    for order_id in ids:
        if order_id in _excluded_ids:
            continue
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
        # Paging decisions use the raw page; only allowed-branch rows are kept (and so cached/hydrated).
        raw_page = payload.get("salesorders") or []
        page_records = branches.filter_allowed(raw_page)
        raw_count = payload.get("raw_count", len(raw_page))  # pre-filter size when zoho_client already filtered
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
        if not raw_count or not has_more or raw_count < 200:
            break
        page += 1
    return records


def _wide_bounds(today: date | None = None) -> tuple[date, date]:
    today = today or datetime.now(PHT).date()
    return today - timedelta(days=WIDE_WINDOW_PAST_DAYS), today + timedelta(days=WIDE_WINDOW_FUTURE_DAYS)


def _wide_key() -> tuple[str, str]:
    start, end = _wide_bounds()
    return start.isoformat(), end.isoformat()


def _prune_windows_locked(cache: dict, now: float | None = None) -> None:
    """Caller holds _window_lock. Never drops the current wide window (see the comment on WINDOW_CACHE_MAX)."""
    now = time.monotonic() if now is None else now
    wide = _wide_key()
    for key in [key for key, (used, _) in cache.items() if key != wide and now - used >= WINDOW_TTL_SECONDS]:
        del cache[key]
    while len(cache) > WINDOW_CACHE_MAX:
        victim = next((key for key in cache if key != wide), None)  # insertion order = least recently used first
        if victim is None:
            break
        del cache[victim]


def _store_window_locked(cache: dict, key: tuple[str, str], rows: dict) -> None:
    cache.pop(key, None)
    cache[key] = (time.monotonic(), rows)
    _prune_windows_locked(cache)


def _read_window_locked(cache: dict, key: tuple[str, str], now: float) -> dict | None:
    entry = cache.pop(key, None)
    if entry is None:
        return None
    cache[key] = (now, entry[1])  # most recently used
    return entry[1]


def _covering_rows_locked(start: date, end: date) -> dict | None:
    """Rows of any cached hydrated window that covers [start, end] (memory only) - the last good data to show while the exact window
    is loading or Zoho is failing."""
    lo, hi = start.isoformat(), end.isoformat()
    for (key_start, key_end), (_, rows) in _windows.items():
        if key_start <= lo and hi <= key_end:
            return rows
    return None


def cached_rows_overlapping(start: date, end: date) -> list[SalesOrderCache]:
    """Rows shipping in [start, end] from every window already in memory. Never calls Zoho."""
    with _window_lock:
        caches = [rows for cache in (_windows, _windows_unhydrated) for _, rows in cache.values()]
    found: dict[str, SalesOrderCache] = {}
    for rows in caches:
        for rid, row in rows.items():
            if row.expected_shipment_date is not None and start <= row.expected_shipment_date <= end:
                found.setdefault(rid, row)
    return [_apply_assignment(row) for row in found.values()]


def _within_wide_window(start: date, end: date) -> bool:
    wide_start, wide_end = _wide_bounds()
    return wide_start <= start and end <= wide_end


def _modified_at(record: dict) -> datetime | None:
    raw = record.get("last_modified_time") or record.get("last_modified_time_formatted")
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _pull_delta_since(start: date, end: date, since: datetime) -> dict[str, dict]:
    """Zoho Inventory code path for delta sync.

    No live probing is done here. The list API already accepts sort_column; public docs for this
    endpoint document sorting, but not a stable last_modified_time filter. We therefore page
    salesorders sorted by last_modified_time descending and stop once a page is older than the
    previous sync watermark.
    """
    records: dict[str, dict] = {}
    page = 1
    while True:
        payload = fetch_sales_orders_by_shipment_date(start, end, page=page, per_page=200, sort_column="last_modified_time")
        rows = payload.get("salesorders") or []
        raw_count = payload.get("raw_count", len(rows))
        oldest: datetime | None = None
        for record in rows:
            modified = _modified_at(record)
            if modified is not None:
                oldest = modified if oldest is None else min(oldest, modified)
            if modified is None or modified > since:
                record_id = str(record.get("salesorder_id") or record.get("id") or "")
                expected_shipment = _as_date(record.get("shipment_date") or record.get("expected_shipment_date"))
                if record_id and expected_shipment and start <= expected_shipment <= end:
                    records[record_id] = record
        context = payload.get("page_context") or {}
        has_more = context.get("has_more_page")
        if isinstance(has_more, str):
            has_more = has_more.strip().lower() == "true"
        if not has_more or raw_count < 200 or (oldest is not None and oldest <= since):
            break
        page += 1
    return records


def _pull_window_legacy(start: date | None, end: date | None) -> dict[str, dict]:
    records: dict[str, dict] = {}
    page = 1
    seen_fingerprints: set[tuple] = set()
    from datetime import timedelta

    lookup_start = start - timedelta(days=90) if start else None
    pages_fetched = 0
    rows_scanned = 0
    while True:
        payload = fetch_sales_orders(date_from=lookup_start, date_to=end, page=page, per_page=200)
        pages_fetched += 1
        # Paging decisions use the raw page; only allowed-branch rows are kept (and so cached/hydrated).
        raw_page = payload.get("salesorders") or []
        page_records = branches.filter_allowed(raw_page)
        raw_count = payload.get("raw_count", len(raw_page))  # pre-filter size when zoho_client already filtered
        rows_scanned += raw_count
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
        if not raw_count or not has_more or raw_count < 200:
            break
        if pages_fetched >= 3:
            break
        page += 1
    logger.warning(
        "[LiveSalesOrderCache] legacy shipment fallback start=%s end=%s pages=%d rows_scanned=%d rows_matched=%d page_cap=3 potentially_truncated=%s",
        start, end, pages_fetched, rows_scanned, len(records),
        bool(pages_fetched >= 3 and has_more and raw_count >= 200),
    )
    return records


def _remember_detail(rid: str, modified: str, fields: dict) -> None:
    with _detail_lock:
        _details.pop(rid, None)
        _details[rid] = (modified, fields)
        while len(_details) > DETAIL_CACHE_MAX:
            _details.pop(next(iter(_details)), None)


def cached_detail_has_acknowledged_sub_status(order_id: str) -> bool:
    """True when this process has ever hydrated this SO detail and that latest detail said
    acknowledged. Used as a hard exclusion signal for Inventory when Zoho's list/custom
    view lags behind detail."""
    with _detail_lock:
        hit = _details.get(str(order_id))
    return bool(hit and record_has_acknowledged_sub_status(hit[1]))


_SHADOW_FIELDS = ("status", "current_sub_status", "shipment_date", "delivery_method")


def _shadow_in_shared_window(record: dict) -> bool:
    expected = _as_date(record.get("shipment_date") or record.get("expected_shipment_date"))
    if expected is None:
        return False
    start, end = _wide_bounds()
    return start <= expected <= end


def _shadow_value(record: dict, field: str):
    if field == "current_sub_status":
        custom = record.get("custom_field_hash") if isinstance(record.get("custom_field_hash"), dict) else {}
        return record.get("current_sub_status") or record.get("order_sub_status") or custom.get("cf_sub_status")
    if field == "line_items":
        return tuple(sorted((str(item.get("item_id") or item.get("item_code") or ""), item.get("quantity"))
                            for item in (record.get("line_items") or []) if isinstance(item, dict)))
    if field == "lock_details.is_locked":
        lock = record.get("lock_details") or record.get("lock_detail") or record.get("lock") or {}
        return lock.get("is_locked") if isinstance(lock, dict) else None
    return record.get(field)


def _shadow_compare(api_record: dict) -> None:
    """Compare API data with the webhook copy; reconcile mode repairs mapping drift."""
    shadow = _shadow_mode()
    reconcile = _webhook_reconcile_mode()
    if (not shadow and not reconcile) or not isinstance(api_record, dict) or not _shadow_in_shared_window(api_record):
        return
    record_id = str(api_record.get("salesorder_id") or api_record.get("sales_order_id") or api_record.get("id") or "")
    if not record_id or not branches.is_allowed(api_record):
        return
    with _detail_lock:
        webhook = _webhook_copies.get(record_id)
    if webhook is None:
        if shadow:
            zoho_usage.record_shadow("shadow_missing")
        return
    webhook_ts, webhook_record = webhook
    api_ts = str(api_record.get("last_modified_time") or "")
    fields = list(_SHADOW_FIELDS) + ["line_items", "lock_details.is_locked"]
    differing = [field for field in fields if _shadow_value(api_record, field) != _shadow_value(webhook_record, field)]
    newer = bool(api_ts and webhook_ts and api_ts > webhook_ts)
    equal_mapping_drift = api_ts == webhook_ts and bool(differing)
    if newer or equal_mapping_drift:
        if shadow:
            zoho_usage.record_shadow("shadow_mismatch")
            logger.warning("[WEBHOOK_SHADOW_MISMATCH] so_id=%s so_number=%s api_ts=%s webhook_ts=%s fields_differing=%s",
                           record_id, api_record.get("salesorder_number") or api_record.get("order_number") or "",
                           api_ts, webhook_ts, differing)
        if reconcile:
            with _detail_lock:
                _webhook_copies[record_id] = (api_ts or webhook_ts, dict(api_record))
            zoho_usage.record_mapping_fix()
            logger.info("[WEBHOOK_MAPPING_FIX] so_id=%s fields=%s", record_id, differing or ["last_modified_time"])
    else:
        if shadow:
            zoho_usage.record_shadow("shadow_ok")


def _hydrate_details(records: dict[str, dict]) -> None:
    # Reuse cached detail for every order whose last_modified_time is unchanged; fetch only
    # the rest. Keep fetching below Zoho's approximate 100 requests/minute ceiling.
    to_fetch: list[str] = []
    for rid, record in records.items():
        _shadow_compare(record)
        modified = str(record.get("last_modified_time") or "")
        with _detail_lock:
            hit = _details.get(rid)
        if modified and hit is not None and hit[0] == modified:
            records[rid] = {**record, **hit[1]}
        else:
            if hit is not None and _shadow_mode() and modified and hit[0] and modified > hit[0]:
                logger.warning("[WEBHOOK_SHADOW_MISMATCH] salesorder_id=%s webhook_last_modified_time=%s api_last_modified_time=%s", rid, hit[0], modified)
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
                _shadow_compare(full)
                fields = {k: v for k, v in full.items() if v not in (None, "", [], {})}
                modified = str(records[rid].get("last_modified_time") or "")
                records[rid] = {**records[rid], **fields}
                if modified:
                    _remember_detail(rid, modified, fields)
                _persist_live_snapshot(records[rid], "api")
            except ZohoError:
                continue


class WindowWarming(Exception):
    """The shared Zoho window (or the assigned SOs) is not in memory yet. A background fill is running: answer 503 and let the caller retry."""


WARM_FAILURE_TTL_SECONDS = 60
_warm_lock = threading.Lock()
_warming: dict[tuple, threading.Thread] = {}
_warm_failed: dict[tuple, tuple[float, Exception]] = {}


FORCE_BLOCKING_READS = False  # test hook; the environment variable below does the same in production


def _blocking_reads_forced() -> bool:
    """Kill switch: LOAD_PLANNING_BLOCKING_READS=true makes Load Planning wait for a cold Zoho fill again (the pre-2026-10-13 behaviour)."""
    return FORCE_BLOCKING_READS or os.environ.get("LOAD_PLANNING_BLOCKING_READS", "").strip().lower() in {"1", "true", "yes"}


def _webhook_reconcile_mode() -> bool:
    return (os.environ.get("ZOHO_WEBHOOKS_ENABLED", "false").strip().lower() == "true"
            and os.environ.get("ZOHO_WEBHOOK_SHADOW", "false").strip().lower() != "true")


def _shadow_mode() -> bool:
    return os.environ.get("ZOHO_WEBHOOK_SHADOW", "false").strip().lower() == "true"


def _start_background_warm(token: tuple, work) -> None:
    """Single-flight: run `work` on a daemon thread unless the same fill is already running. A recent failure is raised to the next caller
    (a ZohoError becomes a readable 503, e.g. the budget guard) instead of retrying a failing fill on every request."""
    with _warm_lock:
        failed = _warm_failed.get(token)
        if failed is not None and time.monotonic() - failed[0] < WARM_FAILURE_TTL_SECONDS:
            raise zoho_resilience.light_error(failed[1])  # a fresh copy: re-raising the stored one would re-attach a traceback to it
        _warm_failed.pop(token, None)
        for stale in [key for key, (at, _) in _warm_failed.items() if time.monotonic() - at >= WARM_FAILURE_TTL_SECONDS]:
            del _warm_failed[stale]
        if token in _warming and _warming[token].is_alive():
            return

        def run():
            try:
                work()
            except Exception as exc:  # noqa: BLE001 - remembered for the next caller (without its traceback: it would pin every frame)
                with _warm_lock:
                    _warm_failed[token] = (time.monotonic(), zoho_resilience.light_error(exc))
                logger.warning("[LiveSalesOrderCache] background fill %s failed: %s", token[0], exc)
            finally:
                with _warm_lock:
                    _warming.pop(token, None)

        thread = threading.Thread(target=contextvars.copy_context().run, args=(run,), name=f"warm-{token[0]}", daemon=True)
        _warming[token] = thread
        thread.start()
    logger.info("[LiveSalesOrderCache] %s not in memory: filling it in the background, this request answers 503 'loading'", token[0])


def _rows_between(rows: dict, start: date, end: date) -> list[SalesOrderCache]:
    return [_apply_assignment(row) for row in rows.values() if row.expected_shipment_date is not None and start <= row.expected_shipment_date <= end]


def get_window(start: date, end: date, *, hydrate: bool = True, block: bool = True) -> list[SalesOrderCache]:
    """Shared live window. The list pull hydrates SO details only for records whose
    last_modified_time is new or changed, then serves unchanged records from memory.

    hydrate=False (a caller that needs only list fields, e.g. the city options) skips the per-order detail fetch for a range outside
    the wide window; such a range is kept apart (_windows_unhydrated) so nobody else is served rows without line items. A range inside
    the wide window is always the shared, hydrated wide window.

    block=False (Load Planning's list endpoints): when the window is not in memory yet, start ONE background fill and raise WindowWarming
    instead of holding the request through a long cold Zoho pull (which the browser reports as a bare CORS / network error). While that
    fill runs or after it failed, a cached window that covers the range is served instead (the request is marked stale)."""
    block = block or _blocking_reads_forced()
    epoch = zoho_acquisition.generation()
    wide_start, wide_end = _wide_bounds()
    wide_key = (wide_start.isoformat(), wide_end.isoformat())
    key = wide_key if _within_wide_window(start, end) else (start.isoformat(), end.isoformat())
    hydrate = hydrate or key == wide_key
    now = time.monotonic()
    with _window_lock:
        for cache in ((_windows,) if hydrate else (_windows, _windows_unhydrated)):
            _prune_windows_locked(cache, now)
            # Explicit date refreshes are reusable even inside the shared wide range.
            exact = _read_window_locked(cache, (start.isoformat(), end.isoformat()), now)
            if exact is not None:
                return _rows_between(exact, start, end)
            rows = _read_window_locked(cache, key, now)
            if rows is not None:
                return _rows_between(rows, start, end)
    if not block:
        failure: Exception | None = None
        try:
            _start_background_warm(("window" if hydrate else "window-lite", *key), lambda: get_window(start, end, hydrate=hydrate))
        except Exception as exc:  # noqa: BLE001 - a remembered failure: answer from a covering window when there is one
            failure = exc
        with _window_lock:
            covering = _covering_rows_locked(start, end)
        if covering is not None:
            zoho_resilience.note_stale(None, "zoho_failed" if failure is not None else "warming")
            return _rows_between(covering, start, end)
        if failure is not None:
            raise failure
        raise WindowWarming("Sales orders are loading from Zoho. Try again in a few seconds.")
    raw = _pull_window(wide_start, wide_end) if key == wide_key else _pull_window(start, end)
    if hydrate:
        _hydrate_details(raw)
    rows = {rid: _build_transient(record) for rid, record in raw.items()}
    _observe_dates(raw.values())
    with zoho_acquisition.publication(epoch) as current:
        if current:
            with _window_lock:
                _store_window_locked(_windows if hydrate else _windows_unhydrated, key, rows)
    memory_debug.log_rss(f"window-built {key[0]}..{key[1]} rows={len(rows)} windows={len(_windows)}")
    return _rows_between(rows, start, end)


def refresh_window(start: date, end: date) -> int:
    """Force only this shipment slice, preserving other cached dates and detail reuse."""
    epoch = zoho_acquisition.generation()
    raw = _pull_window(start, end)
    _hydrate_details(raw)
    rows = {rid: _build_transient(record) for rid, record in raw.items()}
    _observe_dates(raw.values())
    with zoho_acquisition.publication(epoch) as current:
        if current:
            with _window_lock:
                for cache in (_windows, _windows_unhydrated):
                    for key, (_, existing) in list(cache.items()):
                        if key[0] <= end.isoformat() and start.isoformat() <= key[1]:
                            merged = {rid: row for rid, row in existing.items()
                                      if not (row.expected_shipment_date and start <= row.expected_shipment_date <= end)
                                      and rid not in rows}
                            merged.update({rid: row for rid, row in rows.items()
                                           if row.expected_shipment_date and key[0] <= row.expected_shipment_date.isoformat() <= key[1]})
                            _store_window_locked(cache, key, merged)
                _store_window_locked(_windows, (start.isoformat(), end.isoformat()), rows)
            for rid, row in rows.items():
                if rid in _assigned_zoho:
                    _assigned_zoho[rid] = _apply_assignment(row)
    return len(raw)


def refresh_shared_window(*, force_full: bool = False) -> int:
    """Refresh button and schedulers use this instead of clearing caches.

    Full re-pulls are capped to once every two hours and only during working hours. Otherwise,
    a delta page walk updates the shared wide window and stops when records are older than the
    last sync watermark.
    """
    global _wide_window_last_sync, _wide_window_last_full
    start, end = _wide_bounds()
    key = (start.isoformat(), end.isoformat())
    now = datetime.now(timezone.utc)
    mono = time.monotonic()
    working = 6 <= datetime.now(PHT).hour < 20
    with _window_lock:
        existing = _windows.get(key)
    reconcile = _webhook_reconcile_mode()
    if reconcile and existing is not None and _wide_window_last_sync is not None and now - _wide_window_last_sync < timedelta(seconds=RECONCILE_SECONDS):
        return len(existing[1])
    force = force_full if not reconcile else False
    do_full = force or existing is None or (_wide_window_last_sync is None) or (working and mono - _wide_window_last_full >= (RECONCILE_SECONDS if reconcile else FULL_REPULL_SECONDS))
    if not working and existing is not None:
        return len(existing[1])
    raw = _pull_window(start, end) if do_full else _pull_delta_since(start, end, _wide_window_last_sync)
    _hydrate_details(raw)
    with _window_lock:
        current = dict((existing[1] if existing else {}).items())
        for rid, record in raw.items():
            current[rid] = _build_transient(record)
        _store_window_locked(_windows, key, current)
    _observe_dates(raw.values())
    memory_debug.log_rss(f"wide-window-refreshed {'full' if do_full else 'delta'} rows={len(current)} details={len(_details)} assigned_rows={len(_assigned_zoho)}")
    _wide_window_last_sync = now
    if do_full:
        _wide_window_last_full = mono
    return len(raw)


def get_detail(order_id: str, loader=None, *, needs_hybrid: bool = False) -> dict:
    """Return the webhook detail; optionally enrich once for invoice/comments consumers.

    ``needs_hybrid`` is used only by the SO drawer/export paths that need fields Zoho
    does not include in the push body. The cache key includes last_modified_time, so a
    new webhook version gets at most one fallback detail request.
    """
    row = find_cached(order_id)
    raw = getattr(row, "raw_json", None) if row is not None else None
    modified = str(((raw if isinstance(raw, dict) else {}) or {}).get("last_modified_time") or "")
    hybrid_key = (str(order_id), modified)
    if needs_hybrid:
        with _detail_lock:
            hybrid = _hybrid_details.get(hybrid_key)
        if hybrid is not None:
            return {"salesorder": dict(hybrid)}
    if modified:
        with _detail_lock:
            hit = _details.get(str(order_id))
        if hit is not None and hit[0] == modified and not needs_hybrid:
            return {"salesorder": {**(row.raw_json or {}), **hit[1]}}
    detail = (loader or fetch_sales_order_detail)(order_id)
    record = detail.get("salesorder") or detail
    _persist_live_snapshot(record, "api")
    _shadow_compare(record)
    fields = {k: v for k, v in record.items() if v not in (None, "", [], {})}
    if modified:
        _remember_detail(str(order_id), modified, fields)
        if needs_hybrid:
            with _detail_lock:
                _hybrid_details[hybrid_key] = dict(record)
    return detail


def cached_detail(order_id: str) -> dict | None:
    with _detail_lock:
        hit = _details.get(str(order_id))
    if not hit:
        return None
    return dict(hit[1])


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
    if acknowledged:
        modified = str((assigned.raw_json if assigned is not None and isinstance(assigned.raw_json, dict) else {}).get("last_modified_time") or f"ack:{time.monotonic()}")
        _remember_detail(str(order_id), modified, {"current_sub_status": sub_status, "order_sub_status": sub_status})  # bounded like every other entry
    else:
        with _detail_lock:
            _details.pop(str(order_id), None)


def apply_salesorder_webhook(record: dict) -> bool:
    """Apply a newest Sales Order webhook to the live caches.

    Webhooks are the push path for current orders.  This function deliberately stays
    memory-only: the existing history writer remains responsible for durable state
    transitions.  A caller may safely replay an event; ``last_modified_time`` makes
    older deliveries no-ops.
    """
    record_id = str(record.get("salesorder_id") or record.get("sales_order_id") or record.get("id") or "")
    if not record_id:
        return False
    modified = str(record.get("last_modified_time") or "")
    with _detail_lock:
        previous = _details.get(record_id)
    if modified and previous is not None and previous[0] and modified <= previous[0]:
        return False
    record = {**record, "source": "webhook"}
    _persist_live_snapshot(record, "webhook")
    # Zoho's webhook puts fulfillment counters on each line, while the list/report
    # consumers historically read the aggregate order fields. Preserve that shape.
    lines = [item for item in (record.get("line_items") or []) if isinstance(item, dict)]
    for aggregate in ("quantity_packed", "quantity_shipped", "quantity_delivered", "quantity_invoiced"):
        if record.get(aggregate) in (None, "") and lines:
            total = 0.0
            for item in lines:
                try:
                    total += float(item.get(aggregate) or 0)
                except (TypeError, ValueError):
                    continue
            record[aggregate] = total
    closed = str(record.get("status") or record.get("order_status") or "").casefold() in {"fulfilled", "closed", "void"}
    closed = closed or str(record.get("current_sub_status") or record.get("order_sub_status") or "").casefold() in {"closed", "void"}
    new_row = _build_transient(record) if not closed and branches.is_allowed(record) else None
    changed_item_ids = {str(item.get("item_id")) for item in (record.get("line_items") or []) if isinstance(item, dict) and item.get("item_id")}
    with _window_lock:
        found = False
        for _, rows in _windows.values():
            row = rows.get(record_id)
            if row is not None:
                merge_zoho_payload(row, record)
                found = True
        for _, rows in _windows_unhydrated.values():
            row = rows.get(record_id)
            if row is not None:
                merge_zoho_payload(row, record)
                found = True
        if closed:
            for cache in (_windows, _windows_unhydrated):
                for _, rows in cache.values():
                    rows.pop(record_id, None)
        elif new_row is not None and not found:
            start, end = _wide_bounds()
            if new_row.expected_shipment_date and start <= new_row.expected_shipment_date <= end:
                key = (start.isoformat(), end.isoformat())
                existing = _windows.get(key)
                if existing is None:
                    _windows[key] = (time.monotonic(), {record_id: new_row})
                else:
                    existing[1][record_id] = new_row
    assigned = _assigned_zoho.get(record_id)
    if assigned is not None and not closed:
        merge_zoho_payload(assigned, record)
    _observe_dates([record])
    if modified:
        _remember_detail(record_id, modified, {k: v for k, v in record.items() if v not in (None, "", [], {})})
        with _detail_lock:
            _webhook_copies[record_id] = (modified, {k: v for k, v in record.items() if v not in (None, "", [], {})})
        with _detail_lock:
            for key in [key for key in _hybrid_details if key[0] == record_id and key[1] != modified]:
                _hybrid_details.pop(key, None)
    if changed_item_ids:
        try:
            from services import item_detail_cache
            item_detail_cache.invalidate_items(changed_item_ids)
        except Exception:  # cache invalidation must never reject a webhook
            logger.debug("item cache invalidation skipped", exc_info=True)
    return True


def apply_lock_webhook(record: dict) -> bool:
    """Merge a lock-result payload into the cached SO without a Zoho read."""
    order_id = str(record.get("salesorder_id") or record.get("sales_order_id") or record.get("id") or "")
    if not order_id:
        return False
    lock = record.get("lock_details") or record.get("lock_detail") or record.get("lock") or record
    if not isinstance(lock, dict):
        return False
    _persist_live_snapshot({"salesorder_id": order_id, "lock_details": lock, "last_modified_time": record.get("last_modified_time")}, "webhook")
    for row in list(_assigned_zoho.values()):
        if str(getattr(row, "id", "")) == order_id:
            raw = row.raw_json if isinstance(row.raw_json, dict) else {}
            merge_zoho_payload(row, {"lock_details": lock, "lock_detail": {"lock_details": lock}, "source": "webhook"})
            row.raw_json = {**raw, "lock_details": lock, "lock_detail": {"lock_details": lock}, "source": "webhook"}
            return True
    return False


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
    if transfer_orders.is_to_id(order_id) or purchase_orders.is_po_id(order_id):
        return None  # a TO / PO is only ever held (with its lines) in _assigned_zoho; ensure_zoho_data loads it
    with _window_lock:
        for _, records in _windows.values():
            if order_id in records:
                return records[order_id]
    return None
