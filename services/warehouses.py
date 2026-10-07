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
