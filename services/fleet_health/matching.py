"""RareChain vehicle <-> Cartrack vehicle matching, by normalised plate (uppercase, no spaces/dashes).

kind:
  tracked      - has a Cartrack match (gets daily stats)
  third_party  - not maintained by RGF (vehicles.is_third_party): no service bars / risk / eco score, left out of KPIs
  no_tracker   - everything else without a Cartrack match (e.g. the motorcycles): shown as "No tracker", never zeros
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.vehicle import Vehicle
from services import cartrack_client


def normalize_plate(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def classify(vehicles: list[dict], cartrack_roster: list[dict]) -> dict:
    """vehicles: [{id, plate_no, is_third_party}]; cartrack_roster: Cartrack /rest/vehicles rows."""
    by_plate = {normalize_plate(row.get("registration")): row for row in cartrack_roster}
    fleet = []
    used = set()
    for vehicle in vehicles:
        plate = normalize_plate(vehicle.get("plate_no"))
        match = by_plate.get(plate)
        if match:
            used.add(plate)
            kind = "tracked"
        elif vehicle.get("is_third_party"):
            kind = "third_party"
        else:
            kind = "no_tracker"
        capacity = match.get("fuel_capacity") if match else None
        fleet.append({
            "vehicle_id": vehicle["id"],
            "plate": vehicle.get("plate_no"),
            "plate_key": plate,
            "kind": kind,
            "cartrack_vehicle_id": match.get("vehicle_id") if match else None,
            "registration": match.get("registration") if match else None,
            "fuel_capacity_l": float(capacity) if capacity else None,
        })
    return {
        "fleet": fleet,
        "tracked": [row for row in fleet if row["kind"] == "tracked"],
        "third_party": [row for row in fleet if row["kind"] == "third_party"],
        "no_tracker": [row for row in fleet if row["kind"] == "no_tracker"],
        "cartrack_not_in_rarechain": [row.get("registration") for key, row in by_plate.items() if key not in used],
    }


def load_vehicles(db: Session) -> list[dict]:
    rows = db.execute(select(Vehicle.id, Vehicle.plate_no, Vehicle.is_third_party).order_by(Vehicle.id)).all()
    return [{"id": r[0], "plate_no": r[1], "is_third_party": bool(r[2])} for r in rows]


async def match_fleet(db: Session, *, counter=None) -> dict:
    """One Cartrack call (/rest/vehicles) + one Neon read of the 14-row vehicles table."""
    roster = await cartrack_client.get_json("/rest/vehicles", counter=counter)
    rows = roster.get("data", []) if isinstance(roster, dict) else roster
    return classify(load_vehicles(db), rows)
