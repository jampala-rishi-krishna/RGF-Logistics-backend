from __future__ import annotations

WAREHOUSES = {
    "mets": {
        "id": "mets",
        "name": "Mets Cold Storage",
        "address": "Governors Drive, Brgy. Bancal, Carmona, Cavite",
        "google_place": "Mets Logistics, Inc. Bancal",
        "lat": 14.2907776,
        "lng": 121.0134132,
        "place_id_hex": "0x3397d63dd31497fd:0x3c62e37cf817ce0f",
        "map_url": "https://www.google.com/maps/place/Mets+Logistics,+Inc.+Bancal/@14.2907776,121.0108383,17z/data=!3m1!4b1!4m6!3m5!1s0x3397d63dd31497fd:0x3c62e37cf817ce0f!8m2!3d14.2907776!4d121.0134132!16s%2Fg%2F11bc7qm7w5?entry=ttu&g_ep=EgoyMDI2MDcwOC4wIKXMDSoASAFQAw%3D%3D",
    },
    "glacier": {
        "id": "glacier",
        "name": "Glacier Cold Storage",
        "address": "Amvel Business Park, Ninoy Aquino Ave, Parañaque City",
        "google_place": "Glacier Megafridge Incorporated",
        "lat": 14.4922771,
        "lng": 120.9929815,
        "place_id_hex": "0x3397ce8383e741dd:0x48983200f84d92ae",
        "map_url": "https://www.google.com/maps/place/Glacier+Megafridge+Incorporated/@14.4924905,120.9928574,18.5z/data=!4m10!1m2!2m1!1sglacier+amvel!3m6!1s0x3397ce8383e741dd:0x48983200f84d92ae!8m2!3d14.4922771!4d120.9929815!15sCg1nbGFjaWVyIGFtdmVskgEVY29sZF9zdG9yYWdlX2ZhY2lsaXR54AEA!16s%2Fg%2F11bzsg8pvk?entry=ttu&g_ep=EgoyMDI2MDcwOC4wIKXMDSoASAFQAw%3D%3D",
    },
}


def warehouse_key(name: str | None) -> str | None:
    """'mets' / 'glacier' for a warehouse or location NAME (display grouping and per-line SO warehouses only).
    Transfer orders are matched by Zoho warehouse ID instead - see services/transfer_orders.py."""
    text = str(name or "").casefold()
    if "mets" in text:
        return "mets"
    if "glacier" in text:
        return "glacier"
    return None


_PICKUP_ORDER = {"METS": 0, "GLACIER": 1}


def pickup_label(name: str | None) -> str | None:
    """Short pickup label of a line's Zoho location / warehouse name: "METS" / "GLACIER", any other name as-is, None if blank."""
    key = warehouse_key(name)
    if key:
        return key.upper()
    text = str(name or "").strip()
    return text or None


def line_location(item: dict | None) -> str | None:
    """The warehouse / location name of ONE cached Zoho SO line (location_name, warehouse_name or the nested objects)."""
    if not isinstance(item, dict):
        return None
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    warehouse = item.get("warehouse") if isinstance(item.get("warehouse"), dict) else {}
    name = item.get("location_name") or item.get("warehouse_name") or location.get("location_name") or warehouse.get("warehouse_name")
    return str(name).strip() if name and str(name).strip() else None


def pickup_warehouses(names) -> list[str]:
    """Distinct pickup labels of an order's lines, METS before GLACIER, then any other name alphabetically.
    ["METS", "GLACIER"] = one SO picked from both warehouses."""
    labels = {label for label in (pickup_label(n) for n in names or []) if label}
    return sorted(labels, key=lambda label: (_PICKUP_ORDER.get(label, 2), label))


def list_warehouses() -> list[dict]:
    return [dict(warehouse) for warehouse in WAREHOUSES.values()]


def get_warehouse(warehouse_id: str | None) -> dict:
    key = (warehouse_id or "").strip().lower()
    if key not in WAREHOUSES:
        raise KeyError(key)
    return dict(WAREHOUSES[key])


class ReturnWarehouseRequired(ValueError):
    """Return-to-warehouse was requested without the user choosing which warehouse."""


def resolve_return_warehouse(return_to_warehouse: bool, warehouse_id: str | None) -> dict | None:
    """One-way (unchecked) -> None. Checked -> the warehouse the user picked; never auto-selected.
    Raises ReturnWarehouseRequired if none was chosen, KeyError if the id is unknown."""
    if not return_to_warehouse:
        return None
    if not (warehouse_id or "").strip():
        raise ReturnWarehouseRequired("Choose the return warehouse.")
    return get_warehouse(warehouse_id)
