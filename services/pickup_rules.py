"""Exact pickup eligibility in Manila. No IO; shared by listing, assignment and email."""
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo

PHT = ZoneInfo("Asia/Manila")


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=PHT) if parsed.tzinfo is None else parsed.astimezone(PHT)
    except (TypeError, ValueError):
        return None


def method(record):
    if "cf_shipment_method" in record:
        return record["cf_shipment_method"], "cf_shipment_method"
    for field in record.get("custom_fields") or []:
        if field.get("api_name") == "cf_shipment_method":
            return field.get("value"), field.get("api_name")
    return None, None


def classify(record, today=None):
    today = today or datetime.now(PHT).date()
    try:
        delivery = date.fromisoformat(str(record.get("delivery_date") or "")[:10])
    except ValueError:
        return "excluded"
    if method(record)[0] != "For Pickup" or delivery != today + timedelta(days=1) or str(record.get("status") or "").casefold() != "issued":
        return "excluded"
    created = timestamp(record.get("created_time"))
    if created is None:
        return "excluded"  # never silently treat an unknown creation time as pre-cutoff
    return "scheduled" if created <= cutoff(delivery) else "pahabol"


def cutoff(delivery):
    return datetime.combine(delivery - timedelta(days=1), time(14, 0, 59), PHT)


def changed_after_cutoff(record):
    modified = timestamp(record.get("last_modified_time"))
    try:
        return bool(modified and modified > cutoff(date.fromisoformat(str(record.get("delivery_date"))[:10])))
    except ValueError:
        return False


def decision_key(record):
    return f"{record.get('purchaseorder_id')}|{record.get('delivery_date')}|{record.get('created_time')}"
