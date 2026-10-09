"""In-memory cache of Zoho item detail (package weight + per-warehouse stock).

Weight and warehouse stock both come from the same `items/{id}` call, so they share one
fetch. Entries are served stale-while-revalidate: a request never has to block on Zoho for
an item that has been seen before, and list endpoints can use `get_cached` +
`request_refresh` to return immediately while a background pool fills the gaps (the
Zoho rate limiter still paces every actual HTTP attempt). Memory only - nothing is
persisted, per the database discipline rules.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

from services.zoho_client import ZohoError, fetch_item_detail, fetch_item_details_batch

logger = logging.getLogger("item_detail_cache")

# Warehouse stock moves during the day; weights effectively never do. One TTL governs how
# long an entry counts as fresh - stale entries are still served while a refresh runs.
FRESH_SECONDS = int(os.environ.get("ITEM_DETAIL_FRESH_SECONDS", "900"))
FAILURE_BACKOFF_SECONDS = 60
MAX_ENTRIES = 5000

_lock = threading.Lock()
_items: dict[str, tuple[float, dict]] = {}
_failed: dict[str, float] = {}
_inflight: set[str] = set()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="item-detail")


def _trim(payload) -> dict:
    item = payload.get("item") if isinstance(payload, dict) and isinstance(payload.get("item"), dict) else payload
    item = item if isinstance(item, dict) else {}
    return {
        "package_details": item.get("package_details") or {},
        "warehouses": item.get("warehouses") or item.get("warehouse_stock") or item.get("warehouse_details") or [],
    }


def get_cached(item_id: str) -> tuple[dict | None, bool]:
    """(entry, is_fresh). Never touches Zoho."""
    with _lock:
        cached = _items.get(str(item_id))
    if cached is None:
        return None, False
    return cached[1], (time.monotonic() - cached[0]) < FRESH_SECONDS


def fetch(item_id: str) -> dict | None:
    """Fetch from Zoho now and cache. Returns the new entry, or the stale one (or None)
    if Zoho failed. A failure is remembered briefly so callers don't hammer Zoho."""
    key = str(item_id)
    try:
        entry = _trim(fetch_item_detail(key))
    except ZohoError as exc:
        logger.warning("[ITEM_DETAIL] item=%s unavailable=%s", key, exc)
        with _lock:
            _failed[key] = time.monotonic()
            cached = _items.get(key)
        return cached[1] if cached else None
    with _lock:
        _items[key] = (time.monotonic(), entry)
        _failed.pop(key, None)
        if len(_items) > MAX_ENTRIES:
            for stale_key in list(_items)[: MAX_ENTRIES // 5]:
                _items.pop(stale_key, None)
    return entry


def get(item_id: str, *, allow_fetch: bool = True) -> dict | None:
    entry, fresh = get_cached(item_id)
    if fresh or not allow_fetch:
        return entry
    return fetch(item_id) or entry


def _recently_failed(key: str, now: float) -> bool:
    failed_at = _failed.get(key)
    return failed_at is not None and now - failed_at < FAILURE_BACKOFF_SECONDS


def _refresh_worker(key: str) -> None:
    try:
        fetch(key)
    finally:
        with _lock:
            _inflight.discard(key)


def _batch_refresh_worker(keys: list[str]) -> None:
    try:
        try:
            payload = fetch_item_details_batch(keys)
            items = payload.get("items") if isinstance(payload, dict) else []
            by_id = {}
            for item in items or []:
                if isinstance(item, dict):
                    nested = item.get("item") if isinstance(item.get("item"), dict) else {}
                    item_id = str(
                        item.get("item_id")
                        or item.get("id")
                        or nested.get("item_id")
                        or nested.get("id")
                        or ""
                    )
                    if item_id:
                        by_id[item_id] = _trim(item)
            # Some Zoho tenants silently omit unsupported IDs from the batch result.
            # Complete those gaps individually so one partial batch cannot leave every
            # affected row displaying an unknown stock value.
            missing = [key for key in keys if key not in by_id]
            for key in missing:
                entry = fetch(key)
                if entry is not None:
                    by_id[key] = entry
            now = time.monotonic()
            with _lock:
                for key, entry in by_id.items():
                    _items[key] = (now, entry)
                    _failed.pop(key, None)
        except ZohoError as exc:
            logger.warning("[ITEM_DETAIL] batch unavailable=%s", exc)
            for key in keys:
                fetch(key)
    finally:
        with _lock:
            for key in keys:
                _inflight.discard(key)


def request_refresh(item_ids: Iterable[str]) -> int:
    """Queue a background fetch for every item that isn't fresh (missing or stale).
    Returns how many requested items have NO usable value yet and aren't in a failure
    back-off - i.e. how many a caller is still waiting on."""
    now = time.monotonic()
    waiting = 0
    to_start: list[str] = []
    with _lock:
        for raw_id in dict.fromkeys(str(i) for i in item_ids if i):
            cached = _items.get(raw_id)
            fresh = cached is not None and now - cached[0] < FRESH_SECONDS
            if cached is None and not _recently_failed(raw_id, now):
                waiting += 1
            if fresh or raw_id in _inflight or _recently_failed(raw_id, now):
                continue
            _inflight.add(raw_id)
            to_start.append(raw_id)
    for index in range(0, len(to_start), 100):
        _pool.submit(_batch_refresh_worker, to_start[index : index + 100])
    return waiting


def mark_all_stale() -> None:
    """Refresh button: keep serving current values but treat every entry as expired, so the
    next read re-fetches stock from Zoho in the background."""
    with _lock:
        for key, (_, entry) in list(_items.items()):
            _items[key] = (float("-inf"), entry)


def invalidate() -> None:
    with _lock:
        _items.clear()
        _failed.clear()
