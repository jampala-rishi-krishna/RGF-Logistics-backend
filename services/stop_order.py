"""Order of the stops of one batch, and their coordinates. Memory + Neon rows only: no geocoding, no external call.

Source "optimized": the sequence stored by POST /vehicle/{id}/optimize-stops (memory_tables.route_stops rows carry route_id,
sequence, so_id) - used only when every SO of the batch carries the SAME route_id and every one of them appears in that
route's stops. Otherwise source "assignment": assigned_at, then SO number.
"""
from __future__ import annotations

from datetime import datetime, timezone

from services import memory_tables
from services.sales_order_location import address_object


def _assigned_key(row) -> tuple:
    assigned = row.assigned_at
    if assigned is not None and assigned.tzinfo is None:
        assigned = assigned.replace(tzinfo=timezone.utc)
    return (assigned.timestamp() if assigned else 0.0, str(row.salesorder_number or row.id))


def assignment_order(rows: list) -> list[str]:
    return [row.id for row in sorted(rows, key=_assigned_key)]


def order_for_batch(rows: list) -> tuple[list[str], str]:
    """(SO ids in visiting order, "optimized" | "assignment")."""
    fallback = assignment_order(rows)
    route_ids = {str(row.route_id) for row in rows if row.route_id not in (None, "")}
    if len(route_ids) != 1 or any(row.route_id in (None, "") for row in rows):
        return fallback, "assignment"
    route_id = next(iter(route_ids))
    try:
        stops = memory_tables.route_stops.list(route_id=int(route_id))
    except (TypeError, ValueError):
        return fallback, "assignment"
    sequence: dict[str, int] = {}
    for stop in stops:
        so_id = stop.get("so_id")
        if so_id is not None and stop.get("sequence") is not None:
            sequence[str(so_id)] = int(stop["sequence"])
    if not all(row.id in sequence for row in rows):
        return fallback, "assignment"
    return sorted((row.id for row in rows), key=lambda so_id: sequence[so_id]), "optimized"


def _coord(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def coordinates_of(row) -> tuple[float | None, float | None]:
    """lat/lng from the SO's shipping_address JSON when it carries them (latitude/longitude or lat/lng); else (None, None)."""
    address = address_object(row.shipping_address)
    for lat_key, lng_key in (("latitude", "longitude"), ("lat", "lng")):
        lat, lng = _coord(address.get(lat_key)), _coord(address.get(lng_key))
        if lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180 and not (lat == 0 and lng == 0):
            return lat, lng
    return None, None
