from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
import contextvars
from typing import Iterable

from services import branches, item_detail_cache

logger = logging.getLogger("warehouse_stock")
METS_NAME = "mets cold storage"
GLACIER_NAME = "glacier south rgf"


def _stock_value(value):
    try:
        return float(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _parse_stock(entry: dict | None, item_id: str = "", branch_id: str | None = None) -> dict[str, float | None]:
    """Zoho's own "Available for Sale" figure for the item's main Mets warehouse and its Glacier
    South RGF warehouse, passed through exactly as Zoho reports it (negative, zero or
    positive) - no arithmetic. The separate "Chilled - Mets ..." warehouse is a different
    location and must not replace the main Mets figure.

    With BRANCH_STOCK_MAPPING enabled and a branch that has a rule (services/branches.py), that
    branch's own warehouses are used instead (RGF and MSSI - the only handled branches)."""
    rule = branches.STOCK_RULES.get(str(branch_id or "")) if branches.stock_mapping_enabled() else None
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
        if rule:
            if all(part in name for part in rule["mets"]) and not any(part in name for part in rule.get("mets_exclude", ())) and not name.startswith("chilled"):
                site = "mets"
            elif all(part in name for part in rule["glacier"]):
                site = "glacier"
            else:
                site = None
            if site is None:
                continue
            if value is not None:
                result[site] = value
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


def fetch_item_stock(item_id: str, branch_id: str | None = None) -> dict[str, float | None]:
    """Blocking: fetches from Zoho if the cached value is stale or missing."""
    return _parse_stock(item_detail_cache.get(str(item_id), allow_fetch=True), str(item_id), branch_id)


def cached_item_stock(item_id: str, branch_id: str | None = None) -> dict[str, float | None] | None:
    """Never touches Zoho. None means this item has not been fetched yet."""
    entry, _ = item_detail_cache.get_cached(str(item_id))
    return None if entry is None else _parse_stock(entry, str(item_id), branch_id)


def _line_item_id(item: dict) -> str | None:
    nested = item.get("item") if isinstance(item.get("item"), dict) else {}
    value = item.get("item_id") or item.get("itemid") or nested.get("item_id") or nested.get("id")
    return str(value) if value else None


def order_item_ids(orders) -> dict[str, list[str]]:
    return {
        str(order.id): [i for i in (_line_item_id(item) for item in (getattr(order, "raw_json", {}) or {}).get("line_items") or [] if isinstance(item, dict)) if i]
        for order in orders
    }


def _combine(order_items: dict[str, list[str]], stock_of, order_branch=None) -> dict[str, float | None]:
    """stock_of(item_id, branch_id) -> {"mets", "glacier"}."""
    result: dict[str, float | None] = {}
    for order_id, ids in order_items.items():
        branch_id = (order_branch or {}).get(order_id)
        per_item = [stock_of(item_id, branch_id) or {} for item_id in ids]
        for key in ("mets", "glacier"):
            values = [v for v in (stock.get(key) for stock in per_item) if v is not None]
            result[f"{order_id}:{key}"] = min(values) if values and len(values) == len(ids) else None
    return result


def _order_branches(orders) -> dict[str, str | None]:
    return {str(order.id): branches.branch_id_of(order) for order in orders}


def stock_for_orders(orders) -> dict[str, float | None]:
    """Blocking variant (assignment write path, exports): fetches every item it needs."""
    orders = list(orders)
    order_items = order_item_ids(orders)
    order_branch = _order_branches(orders)
    pairs = {(i, order_branch.get(order_id)) for order_id, ids in order_items.items() for i in ids}
    fetched: dict[tuple, dict] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(contextvars.copy_context().run, fetch_item_stock, item_id, branch_id): (item_id, branch_id) for item_id, branch_id in pairs}
        for future, key in futures.items():
            fetched[key] = future.result()
    return _combine(order_items, lambda item_id, branch_id: fetched.get((item_id, branch_id)), order_branch)


def stock_for_orders_cached(orders, extra_item_ids: Iterable[str] = ()) -> tuple[dict[str, float | None], int]:
    """Non-blocking variant for list endpoints: serves whatever is cached (stale is fine),
    queues a background refresh for the rest, and reports how many items are still
    unknown so the UI can poll until they arrive."""
    orders = list(orders)
    order_items = order_item_ids(orders)
    item_ids = [i for ids in order_items.values() for i in ids]
    waiting = item_detail_cache.request_refresh([*item_ids, *extra_item_ids])
    return _combine(order_items, cached_item_stock, _order_branches(orders)), waiting


def invalidate() -> None:
    item_detail_cache.invalidate()
