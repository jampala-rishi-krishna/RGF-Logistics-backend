from __future__ import annotations

import json


def address_object(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    if isinstance(value, list):
        value = value[0] if value else {}
    return value if isinstance(value, dict) else {}


def find_city(value) -> str | None:
    if isinstance(value, dict):
        for key in ("city", "City", "town", "municipality"):
            if value.get(key):
                return str(value[key]).strip()
        for child in value.values():
            found = find_city(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_city(child)
            if found:
                return found
    elif isinstance(value, str):
        try:
            return find_city(json.loads(value))
        except (TypeError, ValueError):
            return None
    return None


def shipping_city(row) -> str | None:
    # Historical sales-order rows intentionally have no raw_json.  Keep this
    # resolver usable for both live Zoho cache objects and durable history rows.
    raw = getattr(row, "raw_json", None) or {}
    address = address_object(row.shipping_address or raw.get("shipping_address"))
    if not address:
        address = address_object(raw.get("shipping_address_details") or raw.get("customer", {}).get("shipping_address"))
    return find_city(address) or find_city(raw.get("shipping_address") or raw)
