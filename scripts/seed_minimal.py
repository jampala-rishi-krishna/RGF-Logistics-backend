"""One-time, idempotent minimal seed after the 2026-09-24 full DB wipe.

Run manually only - NEVER wired into app startup (see main.py's WRITE FREEZE note).
Seeds exactly: one admin user (forced to change password on first login) and the
known truck roster + capacity profiles. Staff directory is NOT seeded here any
more (2026-09-24, Phase 1) - it lives only in the n8n "Logistics Staff Directory"
DataTable (id akyMRuol1VpZXh1D), fetched by services/staff_directory_cache.py.

Usage: python scripts/seed_minimal.py
Requires SEED_ADMIN_EMAIL / SEED_ADMIN_PASSWORD in backend/.env.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from database import SessionLocal
from models.user import User
from models.vehicle import Vehicle
from models.dispatch_pipeline import VehicleCapacityProfile
from auth.security import hash_password

# capacity_kg=None means "capacity unconfirmed" - not yet measured/confirmed with ops.
VEHICLES = [
    {"plate_no": "DCD8953", "vehicle_type": "truck", "capacity_kg": 1800.0, "is_reefer": True, "note": None},
    {"plate_no": "DCD8954", "vehicle_type": "truck", "capacity_kg": 1800.0, "is_reefer": True, "note": None},
    {"plate_no": "DCD8955", "vehicle_type": "truck", "capacity_kg": 1800.0, "is_reefer": True, "note": None},
    {"plate_no": "NFX5791", "vehicle_type": "truck", "capacity_kg": 1000.0, "is_reefer": False, "note": None},
    {"plate_no": "NAJ6018", "vehicle_type": "truck", "capacity_kg": None, "is_reefer": None, "note": "capacity unconfirmed"},
    {"plate_no": "NAN9911", "vehicle_type": "truck", "capacity_kg": None, "is_reefer": None, "note": "capacity unconfirmed"},
    # Motorcycles are NOT seeded yet - plate numbers were not available at seed time.
    # Add {"plate_no": "<plate>", "vehicle_type": "motorcycle", "capacity_kg": 20.0,
    #      "is_reefer": False, "note": None} for each once you have the plates.
]

def seed() -> None:
    admin_email = os.environ.get("SEED_ADMIN_EMAIL", "").strip()
    admin_password = os.environ.get("SEED_ADMIN_PASSWORD", "").strip()
    dispatcher_email = os.environ.get("SEED_DISPATCHER_EMAIL", "dispatcher@rgf.com").strip()
    dispatcher_password = os.environ.get("SEED_DISPATCHER_PASSWORD", "").strip()
    if not admin_email or not admin_password:
        raise SystemExit("SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD must be set in backend/.env before running this script.")

    with SessionLocal() as db:
        # --- 1. Admin user: full control of every tab (require_role always allows "admin"). ---
        user = db.execute(select(User).where(User.email == admin_email)).scalar_one_or_none()
        if user is None:
            user = User(email=admin_email, full_name="Admin", phone=639170000000, role="admin", status="active", password_hash=hash_password(admin_password), must_change_password=True)
            db.add(user)
            print(f"Created admin user {admin_email} (must change password on first login)")
        else:
            user.role = "admin"
            user.status = "active"
            user.password_hash = hash_password(admin_password)
            user.must_change_password = True
            print(f"Updated existing admin user {admin_email} (must change password on first login)")

        if dispatcher_password:
            dispatcher = db.execute(select(User).where(User.email == dispatcher_email)).scalar_one_or_none()
            if dispatcher is None:
                dispatcher = User(email=dispatcher_email, full_name="Dispatcher", phone=639170000001, role="dispatcher", status="active", password_hash=hash_password(dispatcher_password), must_change_password=False)
                db.add(dispatcher)
                print(f"Created dispatcher user {dispatcher_email}")
            else:
                dispatcher.full_name = "Dispatcher"
                dispatcher.role = "dispatcher"
                dispatcher.status = "active"
                dispatcher.password_hash = hash_password(dispatcher_password)
                dispatcher.must_change_password = False
                print(f"Updated dispatcher user {dispatcher_email}")

        # --- 2. Vehicles + capacity profiles. ---
        for v in VEHICLES:
            vehicle = db.execute(select(Vehicle).where(Vehicle.plate_no == v["plate_no"])).scalar_one_or_none()
            if vehicle is None:
                vehicle = Vehicle(plate_no=v["plate_no"])
                db.add(vehicle)
                db.flush()

            profile = db.execute(select(VehicleCapacityProfile).where(VehicleCapacityProfile.plate_no == v["plate_no"])).scalar_one_or_none()
            if profile is None:
                profile = VehicleCapacityProfile(plate_no=v["plate_no"])
                db.add(profile)
            profile.vehicle_type = v["vehicle_type"]
            profile.rated_capacity_kg = v["capacity_kg"]
            profile.is_reefer = v["is_reefer"]
            profile.capacity_note = v["note"]
            profile.is_gps_tracked = True
            profile.is_third_party = False
        print(f"Seeded {len(VEHICLES)} vehicles + capacity profiles (motorcycles pending plates)")

        db.commit()

    print()
    print("Done. This script runs in a separate process from the backend, so its in-memory")
    print("caches (fleet_static_cache, fleet.py's _fleet_cache) cannot be refreshed from here.")
    print("Restart the backend (or POST /vehicles/refresh as an admin) before relying on")
    print("/vehicles or the Cartrack poller picking up the newly seeded roster.")


if __name__ == "__main__":
    seed()
