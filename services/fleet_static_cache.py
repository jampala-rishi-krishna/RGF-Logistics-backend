from __future__ import annotations

import logging
import re
import threading

from sqlalchemy import select

from database import SessionLocal
from models.vehicle import Vehicle

logger = logging.getLogger("fleet_static_cache")

_lock = threading.Lock()
_plate_to_id: dict[str, int] = {}
_vehicles: list[dict] = []


def _normalize_plate(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def refresh() -> None:
    """Reload the static vehicle roster (id, plate_no, driver_id) from Postgres.

    Called once at startup and whenever a vehicle is added/edited/removed. The
    Cartrack poller never queries the DB itself - it only reads this cache."""
    with SessionLocal() as db:
        rows = db.execute(select(Vehicle)).scalars().all()
        vehicles = [{"id": v.id, "plate_no": v.plate_no, "driver_id": v.driver_id, "is_third_party": v.is_third_party, "is_gps_tracked": v.is_gps_tracked} for v in rows]
    with _lock:
        _vehicles.clear()
        _vehicles.extend(vehicles)
        _plate_to_id.clear()
        _plate_to_id.update({_normalize_plate(v["plate_no"]): v["id"] for v in vehicles})
    logger.info("[FleetStaticCache] Refreshed: %d vehicles", len(vehicles))


def plate_to_id() -> dict[str, int]:
    with _lock:
        return dict(_plate_to_id)


def vehicles() -> list[dict]:
    with _lock:
        return list(_vehicles)
