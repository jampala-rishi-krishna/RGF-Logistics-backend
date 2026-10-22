from __future__ import annotations

import logging
import re
from typing import Iterable

from services import branches, item_detail_cache

logger = logging.getLogger("warehouse_stock")
RGF_BRANCH_ID = "4489499000001322444"
MSSI_BRANCH_ID = "4489499000017295785"


def stock_branch_id(order) -> str | None:
    branch_id = branches.branch_id_of(order)
    if branch_id:
        return branch_id
    number = str(getattr(order, "salesorder_number", "") or "")
    if re.match(r"^\s*MS-SO(?![A-Za-z])", number, re.I):
        return MSSI_BRANCH_ID
    if re.match(r"^\s*(?:SO|WM-SO)(?![A-Za-z])", number, re.I):
        return RGF_BRANCH_ID
    return None


def _parse_stock(entry: dict | None, item_id: str = "", branch_id: str | None = None) -> dict[str, float | None]:
    """Return Zoho's physical Available for Sale field for configured warehouse IDs only.

    Never derive availability, coerce/round the source number, or substitute another warehouse.
    """
    rule = branches.STOCK_RULES.get(str(branch_id or ""))
    result: dict[str, float | None] = {"mets": None, "glacier": None}
    if rule is None:
        if item_id:
            logger.debug("[WAREHOUSE_STOCK] item=%s unmatched_branch=%s", item_id, branch_id)
        return result
    for warehouse in (entry or {}).get("warehouses") or []:
        if not isinstance(warehouse, dict):
            continue
        warehouse_id = str(warehouse.get("warehouse_id") or warehouse.get("id") or "")
        site = next((key for key in ("mets", "glacier") if warehouse_id == rule[key]), None)
        if site is None:
            continue
        result[site] = warehouse.get("warehouse_actual_available_for_sale_stock")
    return result


def fetch_item_stock(item_id: str, branch_id: str | None = None) -> dict[str, float | None]:
    """Fetch only on cache miss/expiry; fresh unknown stock remains None, never zero."""
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


def stock_for_orders(orders) -> dict[str, float | None]:
    """Return blank order-level stock: stock is only meaningful for an individual SKU."""
    orders = list(orders)
    return {
        f"{order.id}:{site}": None
        for order in orders
        for site in ("mets", "glacier")
    }


def stock_for_orders_cached(orders, extra_item_ids: Iterable[str] = ()) -> tuple[dict[str, float | None], int]:
    """Queue line-item stock refreshes; never aggregate SKU stock into an order-level value."""
    orders = list(orders)
    order_items = order_item_ids(orders)
    item_ids = [i for ids in order_items.values() for i in ids]
    waiting = item_detail_cache.request_refresh([*item_ids, *extra_item_ids])
    return ({
        f"{order.id}:{site}": None
        for order in orders
        for site in ("mets", "glacier")
    }, waiting)


def invalidate() -> None:
    item_detail_cache.invalidate()
