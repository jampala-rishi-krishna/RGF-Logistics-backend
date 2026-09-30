"""Seed the documented RGF operating fixture into Neon without touching unrelated rows."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from database import SessionLocal
from models.alert import Alert
from models.order import Customer, Order
from models.optimization import VehicleOperatingProfile
from models.route import LoadManifest, Route, RouteStop
from models.vehicle import Vehicle
from models.user import User
from auth.security import hash_password
from services import live_gps_store

DEPOT_LAT = 14.2907776
DEPOT_LNG = 121.0134132
PLATES = ["DCD8955", "DCD8954", "DCD8953", "NFX5791", "NAJ6018", "NAN9911"]


def seed() -> None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        # Development accounts make the protected API demonstrable. Set
        # SEED_PASSWORD in the local environment before running this script.
        # Existing demo users are repaired as well as newly created, so a stale
        # bcrypt hash cannot permanently lock out the documented login.
        seed_password = os.environ.get("SEED_PASSWORD")
        if seed_password:
            for index, (email, full_name, role) in enumerate(
                (("dispatcher@rgf.test", "RGF Dispatcher", "dispatcher"), ("admin@rgf.test", "RGF Admin", "admin")),
                start=1,
            ):
                user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
                if user is None:
                    user = User(full_name=full_name, email=email, phone=639170000000 + index, role=role, status="active")
                    db.add(user)
                user.full_name = full_name
                user.role = role
                user.status = "active"
                user.password_hash = hash_password(seed_password)

        vehicles = {}
        for index, plate in enumerate(PLATES):
            vehicle = db.execute(select(Vehicle).where(Vehicle.plate_no == plate)).scalar_one_or_none()
            if vehicle is None:
                vehicle = Vehicle(plate_no=plate)
                db.add(vehicle)
                db.flush()
            vehicle.driver_id = f"driver-{index + 1}"
            # Live telemetry is in-memory only (services.live_gps_store), never DB columns.
            live_gps_store.upsert(
                plate,
                lat=DEPOT_LAT + index * 0.012,
                lng=DEPOT_LNG + index * 0.014,
                heading=90 + index * 12,
                speed_kph=42 - index * 5 if index != 2 else 0,
                fuel_pct=82 - index * 7,
                ignition_on=index != 2,
            )
            vehicles[plate] = vehicle

            profile = db.execute(
                select(VehicleOperatingProfile).where(VehicleOperatingProfile.vehicle_id == vehicle.id)
            ).scalar_one_or_none()
            if profile is None:
                profile = VehicleOperatingProfile(vehicle_id=vehicle.id)
                db.add(profile)
            profile.capacity_kg = 3500
            profile.capacity_m3 = 18
            profile.temperature_capability = "ambient,chilled,frozen"
            profile.shift_start = "06:00"
            profile.shift_end = "22:00"
            profile.cost_per_km = 18
            profile.cost_per_hour = 250
            # The two DCD vehicles intentionally exercise live-GPS fallback.
            if plate in {"DCD8953", "DCD8955"}:
                profile.depot_lat = None
                profile.depot_lng = None
            else:
                profile.depot_lat = DEPOT_LAT
                profile.depot_lng = DEPOT_LNG

        customers = []
        customer_data = [
            ("Metro Retail Group", "Cavite", 14.2820, 120.9970),
            ("Southeast Grocers", "Manila", 14.5995, 120.9842),
            ("Island Fresh Markets", "Batangas", 13.7565, 121.0583),
            ("Harbor & Field", "Makati", 14.5547, 121.0244),
        ]
        for name, address, lat, lng in customer_data:
            customer = db.execute(select(Customer).where(Customer.name == name)).scalar_one_or_none()
            if customer is None:
                customer = Customer(name=name)
                db.add(customer)
            customer.address = address
            customer.latitude = lat
            customer.longitude = lng
            customer.consent_status = True
            customers.append(customer)
        db.flush()

        orders = []
        for index, customer in enumerate(customers):
            order = db.execute(select(Order).where(Order.customer_id == str(customer.id))).scalars().first()
            if order is None:
                order = Order(customer_id=str(customer.id))
                db.add(order)
            order.route_id = None
            order.vehicle_id = None
            order.status = "pending"
            order.eta_window_start = now + timedelta(hours=1 + index)
            order.eta_window_end = now + timedelta(hours=3 + index)
            order.cargo_temp_c = 2.5 + index * 0.4
            order.shipment_weight = 420 + index * 85
            order.shipment_weight_unit = "kg"
            order.service_time_min = 20 + index * 5
            orders.append(order)
        db.flush()

        alert_data = [
            ("temperature_breach", "critical", PLATES[0], "Cold compartment exceeded 6°C for 8 minutes"),
            ("route_deviation", "warning", PLATES[2], "Vehicle moved outside the approved corridor"),
            ("extended_idling", "warning", PLATES[4], "Engine idle time exceeds 18 minutes"),
        ]
        for alert_type, severity, plate, message in alert_data:
            vehicle = vehicles[plate]
            existing = db.execute(select(Alert).where(Alert.message == message)).scalar_one_or_none()
            if existing is None:
                db.add(Alert(type=alert_type, severity=severity, vehicle_id=str(vehicle.id), message=message, status="open"))

        route = db.execute(select(Route).where(Route.name == "RGF Optimized Cold Chain Route")).scalar_one_or_none()
        if route is None:
            route = Route(name="RGF Optimized Cold Chain Route", mode="recommended", distance_km=96.4, duration_min=182, cost=1735, status="optimized")
            db.add(route)
            db.flush()
            db.add(LoadManifest(route_id=str(route.id), vehicle_id=str(vehicles["DCD8955"].id), status="draft", cargo_type="chilled"))
            for sequence, (customer, order) in enumerate(zip(customers, orders), start=1):
                db.add(RouteStop(route_id=str(route.id), sequence=sequence, location_name=customer.name, lat=customer.latitude, lng=customer.longitude))
                order.route_id = str(route.id)
                order.vehicle_id = str(vehicles["DCD8955"].id)

        db.commit()
        print(f"seeded vehicles={len(vehicles)} customers={len(customers)} orders={len(orders)} alerts={len(alert_data)} route=1 profiles={len(vehicles)}")


if __name__ == "__main__":
    seed()
