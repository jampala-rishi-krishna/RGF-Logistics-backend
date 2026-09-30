from __future__ import annotations

import logging
from threading import Lock

from services.zoho_client import ZohoError, fetch_item_detail

logger = logging.getLogger("item_weight")
_cache: dict[str, float | None] = {}
_cache_lock = Lock()
_FACTORS = {"kg": 1.0, "kilogram": 1.0, "kilograms": 1.0, "g": 0.001, "gram": 0.001, "grams": 0.001, "lb": 0.45359237, "lbs": 0.45359237, "oz": 0.028349523125}


def calculate_line_weight_kg(quantity, unit, item_id: str | None, *, item: dict | None = None, context: str = "") -> float | None:
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
            # A Sales Order line may contain item_id but not package_details.
            # Only reuse the line payload when it actually contains structured
            # package data; otherwise retrieve the authoritative Zoho Item.
            detail = item if isinstance(item, dict) and item.get("package_details") else fetch_item_detail(key)
            detail = detail.get("item") or detail
            package = detail.get("package_details") or {}
            raw_weight = package.get("weight")
            weight_unit = str(package.get("weight_unit") or "").strip().casefold()
            factor = _FACTORS.get(weight_unit)
            package_weight = float(raw_weight) * factor if raw_weight is not None and factor is not None else None
            if package_weight is None:
                logger.warning("Weight unavailable context=%s item_id=%s reason=missing/unsupported package weight", context, key)
        except (ZohoError, TypeError, ValueError, KeyError) as exc:
            logger.warning("Weight unavailable context=%s item_id=%s reason=%s", context, key, exc)
            package_weight = None
        with _cache_lock:
            # Cache successful structured weights. Do not permanently cache a
            # failed lookup: a later refresh must be able to retry Zoho.
            if package_weight is not None:
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
