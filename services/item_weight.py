from __future__ import annotations

import logging
from threading import Lock

from services import item_detail_cache
from services.zoho_client import ZohoError

logger = logging.getLogger("item_weight")
_cache: dict[str, float | None] = {}
_cache_lock = Lock()
_FACTORS = {"kg": 1.0, "kilogram": 1.0, "kilograms": 1.0, "g": 0.001, "gram": 0.001, "grams": 0.001, "lb": 0.45359237, "lbs": 0.45359237, "oz": 0.028349523125}


def _package_weight(key: str, item: dict | None, context: str, allow_fetch: bool) -> float | None:
    """Per-unit package weight in kg. Reuses the line payload only when it carries structured
    package data; otherwise reads the shared Zoho item cache (which also feeds warehouse
    stock, so one Zoho call serves both)."""
    if isinstance(item, dict) and item.get("package_details"):
        detail = item
    else:
        detail = item_detail_cache.get(key, allow_fetch=allow_fetch)
        if detail is None:
            return None
    package = detail.get("package_details") or {}
    raw_weight = package.get("weight")
    weight_unit = str(package.get("weight_unit") or "").strip().casefold()
    factor = _FACTORS.get(weight_unit)
    try:
        weight = float(raw_weight) * factor if raw_weight is not None and factor is not None else None
    except (TypeError, ValueError):
        weight = None
    if weight is None:
        # Cache-only list reads would otherwise log this on every request for the same item.
        (logger.warning if allow_fetch else logger.debug)("Weight unavailable context=%s item_id=%s reason=missing/unsupported package weight", context, key)
    return weight


def calculate_line_weight_kg(quantity, unit, item_id: str | None, *, item: dict | None = None, context: str = "", allow_fetch: bool = True) -> float | None:
    """allow_fetch=False never calls Zoho: a weight that isn't cached yet is simply None
    (list views use this and fill the gaps from a background refresh)."""
    try:
        qty = float(quantity)
    except (TypeError, ValueError):
        return None
    normalized_unit = str(unit or "").strip().casefold()
    if normalized_unit in {"kg", "kilogram", "kilograms"}:
        return qty
    if isinstance(item_id, dict):
        item_id = item_id.get("item_id") or item_id.get("id")
    if not item_id:
        logger.warning("Weight unavailable context=%s reason=item_id missing", context)
        return None
    key = str(item_id)
    with _cache_lock:
        package_weight = _cache.get(key, "__missing__")
    if package_weight == "__missing__":
        try:
            package_weight = _package_weight(key, item, context, allow_fetch)
        except (ZohoError, TypeError, ValueError, KeyError) as exc:
            logger.warning("Weight unavailable context=%s item_id=%s reason=%s", context, key, exc)
            package_weight = None
        if package_weight is not None:
            # Only successful structured weights are cached permanently; a failed or
            # not-yet-fetched lookup must stay retryable.
            with _cache_lock:
                _cache[key] = package_weight
    return qty * package_weight if package_weight is not None else None


def calculate_order_weight_kg(order) -> float | None:
    def item_id_for(item):
        nested = item.get("item") if isinstance(item.get("item"), dict) else {}
        return item.get("item_id") or item.get("itemid") or nested.get("item_id") or nested.get("id")
    weights = [calculate_line_weight_kg(item.get("quantity"), item.get("unit") or item.get("unit_name"), item_id_for(item), item=item, context=str(order.salesorder_number or order.id)) for item in (order.raw_json or {}).get("line_items", [])]
    if not weights or any(weight is None for weight in weights):
        return None
    return sum(weights)
