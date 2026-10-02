from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
import contextvars
from typing import Iterable

from services import item_detail_cache

logger = logging.getLogger("warehouse_stock")
METS_NAME = "mets cold storage"
GLACIER_NAME = "glacier south rgf"


def _stock_value(value):
    try:
        return float(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _parse_stock(entry: dict | None, item_id: str = "") -> dict[str, float | None]:
    """Zoho's own "Available for Sale" figure for the item's main Mets warehouse and its Glacier
    South RGF warehouse, passed through exactly as Zoho reports it (negative, zero or
    positive) - no arithmetic. The separate "Chilled - Mets ..." warehouse is a different
    location and must not replace the main Mets figure."""
    result: dict[str, float | None] = {"mets": None, "glacier": None}
    for warehouse in (entry or {}).get("warehouses") or []:
        if not isinstance(warehouse, dict):
            continue
        name = str(warehouse.get("warehouse_name") or warehouse.get("name") or "").strip().casefold()
        value = _stock_value(
            warehouse.get(
                "warehouse_available_for_sale_stock",
                warehouse.get("available_for_sale_stock", warehouse.get("available_for_sale")),
            )
        )
        if re.search(r"\(deactivated\)$", name, re.I) or name.startswith("(do not use)"):
            continue
        if METS_NAME in name and "near-expiry" not in name and "for supermarket" not in name and not name.startswith("chilled"):
            site = "mets"
        elif GLACIER_NAME in name:
            site = "glacier"
        else:
            if name:
                logger.debug("[WAREHOUSE_STOCK] item=%s unmatched_warehouse=%s", item_id, name)
            continue
        if value is not None:
            result[site] = value
    return result


def fetch_item_stock(item_id: str) -> dict[str, float | None]:
    """Blocking: fetches from Zoho if the cached value is stale or missing."""
    return _parse_stock(item_detail_cache.get(str(item_id), allow_fetch=True), str(item_id))


def cached_item_stock(item_id: str) -> dict[str, float | None] | None:
    """Never touches Zoho. None means this item has not been fetched yet."""
    entry, _ = item_detail_cache.get_cached(str(item_id))
    return None if entry is None else _parse_stock(entry, str(item_id))


def _line_item_id(item: dict) -> str | None:
    nested = item.get("item") if isinstance(item.get("item"), dict) else {}
    value = item.get("item_id") or item.get("itemid") or nested.get("item_id") or nested.get("id")
    return str(value) if value else None


def order_item_ids(orders) -> dict[str, list[str]]:
    return {
        str(order.id): [i for i in (_line_item_id(item) for item in (getattr(order, "raw_json", {}) or {}).get("line_items") or [] if isinstance(item, dict)) if i]
        for order in orders
    }


def _combine(order_items: dict[str, list[str]], stock_of) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for order_id, ids in order_items.items():
        for key in ("mets", "glacier"):
            values = [v for v in ((stock_of(item_id) or {}).get(key) for item_id in ids) if v is not None]
            result[f"{order_id}:{key}"] = min(values) if values and len(values) == len(ids) else None
    return result


def stock_for_orders(orders) -> dict[str, float | None]:
    """Blocking variant (assignment write path, exports): fetches every item it needs."""
    order_items = order_item_ids(orders)
    item_ids = {i for ids in order_items.values() for i in ids}
    fetched: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(contextvars.copy_context().run, fetch_item_stock, item_id): item_id for item_id in item_ids}
        for future, item_id in futures.items():
            fetched[item_id] = future.result()
    return _combine(order_items, lambda item_id: fetched.get(item_id))


def stock_for_orders_cached(orders, extra_item_ids: Iterable[str] = ()) -> tuple[dict[str, float | None], int]:
    """Non-blocking variant for list endpoints: serves whatever is cached (stale is fine),
    queues a background refresh for the rest, and reports how many items are still
    unknown so the UI can poll until they arrive."""
    order_items = order_item_ids(orders)
    item_ids = [i for ids in order_items.values() for i in ids]
    waiting = item_detail_cache.request_refresh([*item_ids, *extra_item_ids])
    return _combine(order_items, cached_item_stock), waiting


def invalidate() -> None:
    item_detail_cache.invalidate()
