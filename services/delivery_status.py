"""Shared delivery-status interpretation for assigned sales orders.

Zoho package status is authoritative when package data is present.  A closed
sales order alone is deliberately not treated as delivered.
"""

from __future__ import annotations
import logging

logger = logging.getLogger("delivery_status")


def _norm(value) -> str:
    return str(value or "").strip().casefold()


def sales_order_delivery_status(raw: dict) -> str:
    raw = raw if isinstance(raw, dict) else {}
    package_values: list[str] = []
    for key in ("packages", "shipments"):
        for item in raw.get(key) or []:
            if isinstance(item, dict):
                package_values.extend(_norm(item.get(k)) for k in ("status", "detailed_status", "shipment_status", "shipping_status", "sub_status") if item.get(k) is not None)
    if package_values:
        delivered = sum(value in {"delivered", "fulfilled", "completed"} for value in package_values)
        if delivered == len(package_values):
            return "Delivered"
        if delivered:
            return "Partially delivered"
        if any(value in {"shipped", "in transit", "partial", "partially delivered"} for value in package_values):
            return "Partially delivered"
        return "Pending"
    values = [_norm(raw.get(k)) for k in ("status", "order_status", "shipment_status", "shipping_status", "shipped_status", "sub_status", "current_sub_status") if raw.get(k) is not None]
    if any(v in {"delivered", "fulfilled", "completed"} for v in values):
        return "Delivered"
    if values and all(v == "closed" for v in values):
        return "Unknown"
    if any(v in {"partially delivered", "partial", "shipped", "in transit"} for v in values):
        return "Partially delivered"
    if values:
        return "Pending"
    logger.warning("[DELIVERY_STATUS_UNRESOLVED] raw_status_values=%s", values)
    return "Unknown"


def is_delivered(raw: dict) -> bool:
    return sales_order_delivery_status(raw) == "Delivered"
