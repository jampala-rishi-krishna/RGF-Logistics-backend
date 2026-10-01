from __future__ import annotations

import contextvars
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.zoho_client import ZohoError, fetch_item_detail

logger = logging.getLogger("warehouse_stock")
TTL_SECONDS = 300
METS_NAME = "mets cold storage"
GLACIER_NAME = "glacier south rgf"
_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, float | None]]] = {}


def _stock_value(value):
    try:
        return float(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def fetch_item_stock(item_id: str) -> dict[str, float | None]:
    now = time.monotonic()
    with _lock:
        cached = _cache.get(str(item_id))
        if cached and now - cached[0] < TTL_SECONDS:
            return dict(cached[1])
    try:
        payload = fetch_item_detail(str(item_id))
    except ZohoError as exc:
        logger.warning("[WAREHOUSE_STOCK] item=%s unavailable=%s", item_id, exc)
        return {"mets": None, "glacier": None}
    item = payload.get("item") if isinstance(payload, dict) else payload
    item = item if isinstance(item, dict) else {}
    warehouses = item.get("warehouses") or item.get("warehouse_stock") or item.get("warehouse_details") or []
    result: dict[str, float | None] = {"mets": None, "glacier": None}
    for warehouse in warehouses:
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
        if METS_NAME in name and "near-expiry" not in name and "for supermarket" not in name:
            result["mets"] = value
        elif GLACIER_NAME in name:
            result["glacier"] = value
        elif name:
            logger.debug("[WAREHOUSE_STOCK] item=%s unmatched_warehouse=%s", item_id, name)
    with _lock:
        _cache[str(item_id)] = (now, result)
    return dict(result)


def stock_for_orders(orders) -> dict[str, float | None]:
    item_ids: set[str] = set()
    order_items: dict[str, list[str]] = {}
    for order in orders:
        ids = []
        for item in (getattr(order, "raw_json", {}) or {}).get("line_items") or []:
            nested = item.get("item") if isinstance(item.get("item"), dict) else {}
            item_id = item.get("item_id") or item.get("itemid") or nested.get("item_id") or nested.get("id")
            if item_id:
                ids.append(str(item_id)); item_ids.add(str(item_id))
        order_items[str(order.id)] = ids
    fetched: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(contextvars.copy_context().run, fetch_item_stock, item_id): item_id for item_id in item_ids}
        for future in as_completed(futures):
            fetched[futures[future]] = future.result()
    result = {}
    for order_id, ids in order_items.items():
        for key in ("mets", "glacier"):
            values = [fetched[item_id][key] for item_id in ids if fetched.get(item_id, {}).get(key) is not None]
            result[f"{order_id}:{key}"] = min(values) if values and len(values) == len(ids) else None
    return result


def invalidate() -> None:
    with _lock:
        _cache.clear()
