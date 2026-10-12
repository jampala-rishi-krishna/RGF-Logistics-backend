"""Delivery notes + on-site contact for the driver app, taken from an SO detail that is ALREADY in memory.

Called from sync_history_row (the assignment write), so the two columns ride in the same UPDATE as the assignment:
no extra write, and never a Zoho call - when the detail is not cached at that moment both stay NULL.
"""
from __future__ import annotations

from services import live_sales_order_cache
from services.sales_order_location import address_object


def _text(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _contact_persons(raw: dict) -> list[dict]:
    for key in ("contact_person_details", "contact_persons_associated", "contact_persons_details", "contact_persons"):
        found = raw.get(key)
        if isinstance(found, list):
            people = [p for p in found if isinstance(p, dict)]
            if people:
                return people
    return []


def extract(raw: dict | None) -> tuple[str | None, dict | None]:
    """(notes, contact) from a Zoho SO detail dict. `contact` is None only when `raw` is not a cached detail (no line
    items); a cached SO with no contact info still gets a dict with null fields, so "not cached" and "nothing to
    show" stay distinguishable."""
    if not isinstance(raw, dict) or not raw.get("line_items"):
        return None, None
    shipping = address_object(raw.get("shipping_address"))
    person = next(iter(_contact_persons(raw)), {})
    person_name = " ".join(p for p in (_text(person.get("first_name")), _text(person.get("last_name"))) if p) or None
    attention = _text(shipping.get("attention"))
    contact = {
        "person": person_name or attention,
        "phone": _text(shipping.get("phone")) or _text(person.get("mobile")) or _text(person.get("phone")),
        "attention": attention,
    }
    return _text(raw.get("notes")), contact


def from_order(order) -> tuple[str | None, dict | None]:
    """Same, for a live-cache order: its own raw_json, completed from the memory-only detail cache."""
    raw = dict(getattr(order, "raw_json", None) or {})
    detail = live_sales_order_cache.cached_detail(getattr(order, "id", ""))
    if detail:
        raw = {**detail, **raw}
    return extract(raw)
