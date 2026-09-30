from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

os.environ.setdefault("CARTRACK_USERNAME", "")
os.environ.setdefault("CARTRACK_API_KEY", "")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main

main.CARTRACK_CONFIGURED = False

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from auth.security import create_access_token
from database import SessionLocal
from models.admin import AuditLog
from models.optimization import OptimizationRun, OptimizationRunRoute, OptimizationRunStop, VehicleOperatingProfile
from models.order import Customer, Order
from models.route import LoadManifest, ManifestItem, Route, RouteStop
from models.user import User
from models.vehicle import Vehicle
from services import live_gps_store
from routers import optimization as optimization_router


PREFIX = f"INTELLIFLEET_E2E_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"


def count_temp(db):
    customer_count = db.query(Customer).filter(Customer.name.like(f"{PREFIX}%")).count()
    customer_ids = [str(c.id) for c in db.query(Customer).filter(Customer.name.like(f"{PREFIX}%")).all()]
    order_ids = [o.id for o in db.query(Order).filter(Order.customer_id.in_(customer_ids)).all()]
    return {"customers": customer_count, "orders": len(order_ids), "order_ids": order_ids}


def response_summary(response):
    try:
        body = response.json()
    except Exception:
        body = response.text[:200]
    return {"status": response.status_code, "body": body}


def verify_route_graph(db, route_id, order_ids, vehicle_id):
    route = db.get(Route, route_id)
    stops = db.execute(select(RouteStop).where(RouteStop.route_id == str(route_id)).order_by(RouteStop.sequence)).scalars().all()
    manifest = db.execute(select(LoadManifest).where(LoadManifest.route_id == str(route_id))).scalars().first()
    orders = [db.get(Order, order_id) for order_id in order_ids]
    return {
        "route": route is not None,
        "stops": len(stops),
        "sequences": [s.sequence for s in stops],
        "manifest": manifest is not None,
        "orders_route_linked": all(order and order.route_id == str(route_id) for order in orders),
        "orders_vehicle_linked": all(order and order.vehicle_id == str(vehicle_id) for order in orders),
        "geometry": bool(route and route.polyline_geojson),
        "provider": "mapbox" if route and route.polyline_geojson else "unknown",
    }


def main_test():
    db = SessionLocal()
    created = {"customer": None, "orders": [], "vehicle": None, "profile": None, "run_ids": [], "route_ids": []}
    before_real = db.execute(text("SELECT id, status, route_id, vehicle_id, shipment_weight, shipment_weight_unit, service_time_min FROM orders WHERE status = 'pending' AND (route_id IS NULL OR route_id = '') ORDER BY id")).mappings().all()
    try:
        user = db.execute(select(User).where(User.role.in_(["dispatcher", "admin"])).order_by(User.id)).scalars().first()
        if user is None:
            raise RuntimeError("No dispatcher/admin user available for the API test")

        customer = Customer(
            name=f"{PREFIX}_CUSTOMER",
            email=f"{PREFIX.lower()}@example.test",
            address="Makati City, Metro Manila, Philippines",
            consent_status=True,
            latitude=14.5547,
            longitude=121.0244,
        )
        vehicle = Vehicle(
            plate_no=f"{PREFIX}_TRUCK",
            driver_id=f"{PREFIX}_DRIVER",
        )
        db.add_all([customer, vehicle])
        db.flush()
        # Live telemetry is in-memory only (services.live_gps_store), never DB columns.
        live_gps_store.upsert(
            f"{PREFIX}_TRUCK",
            lat=14.5995,
            lng=120.9842,
            heading=0,
            speed_kph=0,
            fuel_pct=75,
            ignition_on=True,
        )
        profile = VehicleOperatingProfile(
            vehicle_id=vehicle.id,
            capacity_kg=1000,
            capacity_m3=20,
            temperature_capability="ambient",
            shift_start="00:00",
            shift_end="23:59",
            cost_per_km=10,
            cost_per_hour=300,
            depot_lat=14.5995,
            depot_lng=120.9842,
        )
        orders = [
            Order(customer_id=str(customer.id), status="pending", eta_window_start=datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc), eta_window_end=datetime(2026, 9, 3, 23, 59, tzinfo=timezone.utc), shipment_weight=125, shipment_weight_unit="kg", service_time_min=20),
            Order(customer_id=str(customer.id), status="pending", eta_window_start=datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc), eta_window_end=datetime(2026, 9, 3, 23, 59, tzinfo=timezone.utc), shipment_weight=80, shipment_weight_unit="kg", service_time_min=10),
        ]
        db.add_all([profile, *orders])
        db.commit()
        for order in orders:
            db.refresh(order)
        db.refresh(vehicle)
        created.update(customer=customer.id, orders=[order.id for order in orders], vehicle=vehicle.id, profile=profile.id)

        token = create_access_token(user_id=user.id, role=user.role, email=user.email)
        headers = {"Authorization": f"Bearer {token}"}

        with TestClient(main.app, raise_server_exceptions=False) as client:
            preview = client.post("/optimization/preview", headers=headers, json={"objective": "fastest", "vehicleIds": [vehicle.id], "orderIds": created["orders"]})
            preview_result = response_summary(preview)
            if preview.status_code != 200 or not preview.json().get("feasible"):
                raise RuntimeError(f"Preview failed: {preview_result}")
            plan = preview.json()
            run_id = plan["runId"]
            created["run_ids"].append(run_id)

            apply = client.post("/optimization/apply", headers=headers, json={"runId": run_id})
            apply_result = response_summary(apply)
            if apply.status_code != 200:
                raise RuntimeError(f"Apply failed: {apply_result}")
            route_id = apply.json()["routes"][0]["routeId"]
            created["route_ids"].append(route_id)

            db.expire_all()
            graph = verify_route_graph(db, route_id, created["orders"], vehicle.id)
            run = db.get(OptimizationRun, run_id)
            audit = db.execute(select(AuditLog).where(AuditLog.action == "apply_optimization", AuditLog.target_id == str(run_id))).scalars().all()

            # Stale-plan test: preview, change only temporary operational data, then apply.
            for order in orders:
                order.route_id = None
                order.vehicle_id = None
            orders[0].service_time_min = 25
            db.commit()
            stale_preview = client.post("/optimization/preview", headers=headers, json={"objective": "fastest", "vehicleIds": [vehicle.id], "orderIds": created["orders"]})
            stale_run_id = stale_preview.json().get("runId")
            created["run_ids"].append(stale_run_id)
            orders[0].service_time_min = 30
            db.commit()
            stale_apply = client.post("/optimization/apply", headers=headers, json={"runId": stale_run_id})
            stale_result = response_summary(stale_apply)
            stale_run = db.get(OptimizationRun, stale_run_id)

            # Rollback test: fail in the route-geometry call after route/stop rows are flushed.
            for order in orders:
                order.route_id = None
                order.vehicle_id = None
            orders[0].service_time_min = 20
            db.commit()
            rollback_preview = client.post("/optimization/preview", headers=headers, json={"objective": "fastest", "vehicleIds": [vehicle.id], "orderIds": created["orders"]})
            rollback_run_id = rollback_preview.json().get("runId")
            created["run_ids"].append(rollback_run_id)
            original_polyline = optimization_router.fetch_route_polyline

            async def controlled_failure(_coords):
                raise RuntimeError("controlled E2E failure")

            optimization_router.fetch_route_polyline = controlled_failure
            try:
                rollback_apply = client.post("/optimization/apply", headers=headers, json={"runId": rollback_run_id})
            finally:
                optimization_router.fetch_route_polyline = original_polyline
            rollback_result = response_summary(rollback_apply)
            db.expire_all()
            rollback_run = db.get(OptimizationRun, rollback_run_id)
            rollback_routes = db.execute(select(OptimizationRunRoute).where(OptimizationRunRoute.run_id == rollback_run_id)).scalars().all()
            rollback_persisted_routes = db.execute(select(Route).where(Route.id.in_(created["route_ids"]))).scalars().all()

            print(json.dumps({
                "prefix": PREFIX,
                "before_real_pending": [dict(row) for row in before_real],
                "temporary": {"customer_id": customer.id, "order_ids": created["orders"], "vehicle_id": vehicle.id},
                "preview": {
                    "http_status": preview.status_code,
                    "feasible": plan.get("feasible"),
                    "run_id": run_id,
                    "routing": plan.get("routing"),
                    "weight_kg": {str(orders[0].id): 125, str(orders[1].id): 80},
                    "service_time_min": {str(orders[0].id): 20, str(orders[1].id): 10},
                    "vehicle_capacity_kg": 1000,
                    "routes": plan.get("routes"),
                    "cost_returned": bool(plan.get("totalDistanceKm") is not None),
                    "diagnostics_returned": "warnings" in plan,
                },
                "apply": {"http_status": apply.status_code, "response": apply.json(), "run_status": run.status if run else None},
                "persistence": graph,
                "audit": {"count": len(audit), "success_record": len(audit) == 1},
                "stale_plan": {"http_status": stale_apply.status_code, "expected_409": stale_apply.status_code == 409, "run_status": stale_run.status if stale_run else None},
                "rollback": {
                    "http_status": rollback_apply.status_code,
                    "expected_failure": rollback_apply.status_code >= 500,
                    "run_status": rollback_run.status if rollback_run else None,
                    "run_routes_unchanged": len(rollback_routes) == 1,
                    "no_new_route_from_failed_apply": len(rollback_persisted_routes) == 1,
                    "orders_still_point_to_success_route": all(db.get(Order, order_id).route_id == str(route_id) for order_id in created["orders"]),
                },
            }, default=str))
    finally:
        # Delete only records carrying this run's unique IDs, in dependency order.
        db.rollback()
        for route_id in created["route_ids"]:
            manifest_ids = [str(row.id) for row in db.execute(select(LoadManifest).where(LoadManifest.route_id == str(route_id))).scalars().all()]
            for item in db.execute(select(ManifestItem).where(ManifestItem.manifest_id.in_(manifest_ids))).scalars().all():
                db.delete(item)
            for manifest in db.execute(select(LoadManifest).where(LoadManifest.route_id == str(route_id))).scalars().all():
                db.delete(manifest)
            for stop in db.execute(select(RouteStop).where(RouteStop.route_id == str(route_id))).scalars().all():
                db.delete(stop)
            route = db.get(Route, route_id)
            if route:
                db.delete(route)
        for run_id in created["run_ids"]:
            for row in db.execute(select(OptimizationRunStop).where(OptimizationRunStop.run_id == run_id)).scalars().all():
                db.delete(row)
            for row in db.execute(select(OptimizationRunRoute).where(OptimizationRunRoute.run_id == run_id)).scalars().all():
                db.delete(row)
            for row in db.execute(select(AuditLog).where(AuditLog.target_entity == "optimization_run", AuditLog.target_id == str(run_id))).scalars().all():
                db.delete(row)
            run = db.get(OptimizationRun, run_id)
            if run:
                db.delete(run)
        for order_id in created["orders"]:
            order = db.get(Order, order_id)
            if order:
                db.delete(order)
        if created["profile"]:
            profile = db.get(VehicleOperatingProfile, created["profile"])
            if profile:
                db.delete(profile)
        if created["vehicle"]:
            vehicle = db.get(Vehicle, created["vehicle"])
            if vehicle:
                db.delete(vehicle)
        if created["customer"]:
            customer = db.get(Customer, created["customer"])
            if customer:
                db.delete(customer)
        db.commit()
        after_real = db.execute(text("SELECT id, status, route_id, vehicle_id, shipment_weight, shipment_weight_unit, service_time_min FROM orders WHERE status = 'pending' AND (route_id IS NULL OR route_id = '') ORDER BY id")).mappings().all()
        temp_left = count_temp(db)
        print(json.dumps({"cleanup": temp_left, "real_pending_unchanged": [dict(row) for row in before_real] == [dict(row) for row in after_real], "after_real_pending": [dict(row) for row in after_real]}, default=str))
        db.close()


if __name__ == "__main__":
    main_test()
