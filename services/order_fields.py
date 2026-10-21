"""Delivery method + Fulfillment type, read from a Zoho order record already in memory (list/detail cache). Never calls Zoho.

- delivery_method: Zoho's standard sales-order field `delivery_method` (also `shipment_method` / `delivery_method_name` on some payloads).
- fulfillment_type: the org's custom field `cf_fulfillment_type` (label "Fulfillment Type"). Zoho exposes it as a flat `cf_*` key on list
  records, inside `custom_field_hash`, and inside `custom_fields` (api_name/label + value) on detail records - all three are checked,
  plus a label match on /fulfil(l)ment type/i in case the api_name differs.

Both return a trimmed string, or None when the order has no value (TO / PO records normally have neither)."""
from __future__ import annotations

import re

_FULFILLMENT_LABEL = re.compile(r"fulfil+ment[\s_-]*type", re.I)
_FULFILLMENT_KEYS = ("cf_fulfillment_type", "cf_fulfilment_type", "fulfillment_type")


def _clean(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("label") or value.get("value")
    if value is None or isinstance(value, (list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def _raw(row_or_record) -> dict:
    if isinstance(row_or_record, dict):
        return row_or_record
    raw = getattr(row_or_record, "raw_json", None)
    return raw if isinstance(raw, dict) else {}


def delivery_method(row_or_record) -> str | None:
    record = _raw(row_or_record)
    for key in ("delivery_method", "delivery_method_name", "shipment_method"):
        value = _clean(record.get(key))
        if value:
            return value
    if not isinstance(row_or_record, dict):
        return _clean(getattr(row_or_record, "delivery_method", None))
    return None


def fulfillment_type(row_or_record) -> str | None:
    record = _raw(row_or_record)
    for key in _FULFILLMENT_KEYS:
        value = _clean(record.get(key))
        if value:
            return value
    hashed = record.get("custom_field_hash")
    if isinstance(hashed, dict):
        for key in _FULFILLMENT_KEYS:
            value = _clean(hashed.get(key))
            if value:
                return value
        for key, raw_value in hashed.items():
            if str(key).startswith("cf_") and not str(key).endswith("_unformatted") and _FULFILLMENT_LABEL.search(str(key)):
                value = _clean(raw_value)
                if value:
                    return value
    for field in record.get("custom_fields") or []:
        if not isinstance(field, dict):
            continue
        names = " ".join(str(field.get(k) or "") for k in ("api_name", "label", "placeholder"))
        if _FULFILLMENT_LABEL.search(names):
            value = _clean(field.get("value_formatted") or field.get("value") or field.get("formatted_value"))
            if value:
                return value
    for key, raw_value in record.items():
        if str(key).startswith("cf_") and not str(key).endswith("_unformatted") and _FULFILLMENT_LABEL.search(str(key)):
            value = _clean(raw_value)
            if value:
                return value
    return None


def apply(result: dict, row_or_record) -> dict:
    """Set both keys on a summary dict (always present, None when missing)."""
    result["delivery_method"] = delivery_method(row_or_record) or _clean(result.get("delivery_method"))
    result["fulfillment_type"] = fulfillment_type(row_or_record)
    return result
