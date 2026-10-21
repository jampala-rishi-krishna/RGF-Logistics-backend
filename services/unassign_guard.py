"""Assignment guards shared by Load Planning and Confirmed Orders (no DB writes, no Zoho calls).

* Unassign guard: a MANUAL unassign is refused when Zoho already shows the sales order as fulfilled / shipped /
  partially delivered (or partially shipped) / delivered, or when our trip shows the stop as checked out. Automatic
  unassigns (trip end not-delivered/cancelled, Zoho date change) pass `automatic=True` and bypass this guard.
* Pickup guard: a customer pickup (Delivery method or Fulfillment type says pickup) cannot be assigned to a truck.

Everything here reads values already in memory (the cached Zoho SO dict / the order row, and the order tracker's
in-memory trip facts). The authoritative checked-out check at unassign time stays in dispatch_changes.describe (the
existing trip read of the selected orders' batches).
"""
from __future__ import annotations

import re

# Zoho statuses (status / order_status / shipped_status / shipment_status / delivered_status / sub statuses),
# normalised: lower case, "_" and "-" read as spaces.
_BLOCKED_STATUSES = {
    "fulfilled": "fulfilled",
    "shipped": "shipped",
    "partially shipped": "partially shipped",
    "partially delivered": "partially delivered",
    "partial": "partially delivered",
    "delivered": "delivered",
}
_STATUS_KEYS = ("status", "order_status", "shipped_status", "shipment_status", "shipping_status", "delivered_status", "delivery_status", "sub_status", "current_sub_status")
_ROW_KEYS = ("order_status", "shipment_status", "delivery_status")

CHECKED_OUT_REASON = "The stop was already checked out on the trip. A checked-out order cannot be unassigned."


def _norm(value) -> str:
    return re.sub(r"[\s_\-]+", " ", str(value or "").strip().casefold())


def _raw(order) -> dict:
    raw = order.get("raw_json") if isinstance(order, dict) else getattr(order, "raw_json", None)
    if isinstance(order, dict) and not isinstance(raw, dict):
        return order  # a raw Zoho SO dict itself
    return raw if isinstance(raw, dict) else {}


def _attr(order, key):
    return order.get(key) if isinstance(order, dict) else getattr(order, key, None)


def _order_type(order) -> str:
    try:
        from services import transfer_orders
        return transfer_orders.order_type_of(order)
    except Exception:  # noqa: BLE001 - a plain dict / fake: treat as a sales order
        return "SO"


def zoho_status_blocker(order) -> str | None:
    """Why Zoho's own status forbids a manual unassign (None = Zoho is fine with it). Sales orders (SO / WM) only."""
    if _order_type(order) in {"TO", "PO"}:
        return None
    raw = _raw(order)
    values = [raw.get(key) for key in _STATUS_KEYS]
    values += [_attr(order, key) for key in _ROW_KEYS] if not isinstance(order, dict) else []
    for value in values:
        if isinstance(value, dict):
            value = value.get("status") or value.get("name") or value.get("label")
        label = _BLOCKED_STATUSES.get(_norm(value))
        if label:
            return f"Zoho shows this sales order as {label}. It cannot be unassigned."
    # Zoho package / shipment rows (same reading as services/delivery_status): any shipped or delivered package blocks.
    for key in ("packages", "shipments"):
        for item in raw.get(key) or []:
            if not isinstance(item, dict):
                continue
            for field in ("status", "detailed_status", "shipment_status", "shipping_status", "sub_status"):
                label = _BLOCKED_STATUSES.get(_norm(item.get(field)))
                if label:
                    return f"Zoho shows a package of this sales order as {label}. It cannot be unassigned."
    return None


def checked_out_from_memory(order) -> bool:
    """True when the order tracker's in-memory trip facts (no DB read) show this order's stop as checked out."""
    batch = _attr(order, "assignment_batch_id")
    order_id = _attr(order, "id")
    if not batch or not order_id:
        return False
    try:
        from services import order_tracker_facts
        facts = order_tracker_facts.current(batch) or {}
    except Exception:  # noqa: BLE001
        return False
    stop = (facts.get("stops") or {}).get(str(order_id)) or {}
    return stop.get("status") == "checked_out"


def stop_fact(order) -> dict:
    """`stop_outcome` / `checkout_at` of this order's trip stop from the in-memory trip facts (no DB read); empty when unknown."""
    batch, order_id = _attr(order, "assignment_batch_id"), _attr(order, "id")
    if not batch or not order_id:
        return {}
    try:
        from services import order_tracker_facts
        stop = ((order_tracker_facts.current(batch) or {}).get("stops") or {}).get(str(order_id)) or {}
    except Exception:  # noqa: BLE001 - a display hint must never break a list
        return {}
    if stop.get("status") != "checked_out":
        return {}
    return {"stop_outcome": stop.get("outcome"), "checkout_at": stop.get("checkout_at")}


def unassign_blocked_reason(order) -> str | None:
    """List rows: why the Unassign action must be hidden (memory only). None = Unassign may show."""
    return zoho_status_blocker(order) or (CHECKED_OUT_REASON if checked_out_from_memory(order) else None)


# ---- pickup ----------------------------------------------------------------------------------------------------------

_PICKUP_RE = re.compile(r"pick[\s\-_]*up", re.IGNORECASE)


def fulfillment_type_of(order) -> str | None:
    """Zoho custom field cf_fulfillment_type (services/order_fields reads every shape Zoho uses)."""
    from services import order_fields
    return order_fields.fulfillment_type(order)


def delivery_method_of(order) -> str | None:
    from services import order_fields
    return order_fields.delivery_method(order)


def is_pickup_value(value) -> bool:
    return bool(value) and bool(_PICKUP_RE.search(str(value)))


def pickup_block(order) -> dict | None:
    """The structured "Unable to assign" entry for a customer pickup (None = may be assigned to a truck)."""
    method, fulfillment = delivery_method_of(order), fulfillment_type_of(order)
    if not (is_pickup_value(method) or is_pickup_value(fulfillment)):
        return None
    number = _attr(order, "salesorder_number") or _attr(order, "id")
    date = _attr(order, "expected_shipment_date") or _raw(order).get("shipment_date")
    which = f"Fulfillment type: {fulfillment}" if is_pickup_value(fulfillment) else f"Delivery method: {method}"
    return {
        "id": _attr(order, "id"), "order_number": number, "customer": _attr(order, "customer_name") or _raw(order).get("customer_name"),
        "delivery_date": date.isoformat() if hasattr(date, "isoformat") else date,
        "delivery_method": method, "fulfillment_type": fulfillment,
        "reason": f"{number} is a customer pickup ({which}). Pickup orders are collected by the customer and can't be assigned to a truck.",
    }
