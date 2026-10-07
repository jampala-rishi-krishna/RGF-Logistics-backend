from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from auth.dependencies import CurrentUser, require_role
from models.route import LoadManifest
from services import memory_tables
from services import optimizer
from services.optimization_data import FleetDataError, fetch_fleet_data
from services.google_maps import fetch_route_matrix, fetch_route_polyline, route_requires_ferry
from services.google_route_optimization import GoogleOptimizationError, optimize as google_optimize, parse_response as parse_google_optimization
from services.audit import write_audit_log
from services.time_utils import eta_from_arrival_min
from services.warehouses import ReturnWarehouseRequired

router = APIRouter(prefix="/optimization", tags=["optimization"], dependencies=[Depends(require_role("admin", "dispatcher"))])

# optimization-module/index.js enforces no role gate and writes no audit_log entries on any of
# these routes - ported faithfully (confirmed pre-existing gap, not newly introduced here).

AT_RISK_SLACK_THRESHOLD_MIN = 15


class PreviewBody(BaseModel):
    mode: str | None = None
    vehicleIds: list[int] | None = None
    orderIds: list[int] | None = None
    objective: str = "recommended"
    returnToWarehouse: bool = False
    returnWarehouseId: str | None = None
    avoidTolls: bool = False


class ReoptimizeBody(BaseModel):
    reason: str
    vehicleIds: list[int] | None = None
    orderIds: list[int] | None = None
    objective: str = "recommended"
    returnToWarehouse: bool = False
    returnWarehouseId: str | None = None
    avoidTolls: bool = False


async def _run_optimization(
    db: Session,
    *,
    mode: str,
    vehicle_ids: list | None,
    order_ids: list | None,
    objective: str,
    return_to_warehouse: bool = False,
    return_warehouse_id: str | None = None,
    avoid_tolls: bool = False,
) -> dict:
    try:
        fleet_data = await fetch_fleet_data(db, mode=mode, vehicle_ids=vehicle_ids, order_ids=order_ids, return_to_warehouse=return_to_warehouse, return_warehouse_id=return_warehouse_id)
    except FleetDataError as e:
        raise HTTPException(422, detail={"message": str(e), "issues": e.issues})
    except ReturnWarehouseRequired as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(400, "Unknown return warehouse.")

    # Node indexing: vehicle1.start(0), vehicle1.end(1), vehicle2.start(2), ... then shipments.
    locations: list[list[float]] = []
    for v in fleet_data.vehicles:
        v.start_node = len(locations)
        locations.append([v.start_lng, v.start_lat])
        if v.has_end_location:
            v.end_node = len(locations)
            locations.append([v.end_lng, v.end_lat])
        else:
            v.end_node = v.start_node
    for s in fleet_data.shipments:
        s.node = len(locations)
        locations.append([s.lng, s.lat])

    try:
        matrix = await fetch_route_matrix(locations)
    except Exception as e:
        raise HTTPException(502, f"Road travel data unavailable. Optimization could not be completed. ({e})")

    try:
        google_payload = await google_optimize(fleet_data, avoid_tolls=avoid_tolls)
        google_routes = parse_google_optimization(google_payload, fleet_data)
    except GoogleOptimizationError as e:
        raise HTTPException(502, str(e))
    result = optimizer.OptimizeResponse(
        feasible=bool(google_routes),
        routes=[optimizer.VehicleRoute(**route) for route in google_routes],
        unassigned=[],
        message=None if google_routes else "Google Route Optimization returned no assigned routes.",
    )

    if not result.feasible:
        return {"feasible": False, "message": result.message, "warnings": fleet_data.warnings}

    shipment_by_id = {str(s.id): s for s in fleet_data.shipments}
    vehicle_by_id = {str(v.id): v for v in fleet_data.vehicles}
    node_coords = {v.start_node: [v.start_lng, v.start_lat] for v in fleet_data.vehicles}
    node_coords.update({v.end_node: [v.end_lng, v.end_lat] for v in fleet_data.vehicles if v.has_end_location})
    node_coords.update({s.node: [s.lng, s.lat] for s in fleet_data.shipments})
    for vr in result.routes:
        vehicle = vehicle_by_id.get(str(vr.vehicle_id))
        if vehicle is None:
            continue
        ordered_coords = [node_coords[vehicle.start_node]]
        for stop_id in vr.stop_sequence:
            shipment = shipment_by_id.get(str(stop_id))
            if shipment is not None:
                ordered_coords.append([shipment.lng, shipment.lat])
        if vehicle.has_end_location:
            ordered_coords.append(node_coords[vehicle.end_node])
        ferry_issue = await route_requires_ferry(ordered_coords)
        if ferry_issue:
            return {
                "feasible": False,
                "message": f"Route for vehicle {vehicle.id} cannot be planned as a road-only route: {ferry_issue}",
                "warnings": fleet_data.warnings,
            }

    routing_metadata = {
        "provider": matrix.get("provider"),
        "profile": matrix.get("profile"),
        "traffic_aware": bool(matrix.get("traffic_aware")),
        "calculated_at": matrix.get("calculated_at"),
        "departure_time": matrix.get("departure_time"),
    }
    snapshot = {**fleet_data.data_snapshot, "routing": routing_metadata, "avoid_tolls": avoid_tolls}
    run = memory_tables.optimization_runs.create(
        status="proposed",
        objective=objective,
        mode=mode,
        scope=json.dumps({
            "vehicle_ids": [v.id for v in fleet_data.vehicles],
            "order_ids": [s.order_id for s in fleet_data.shipments],
        }),
        data_snapshot=json.dumps(snapshot),
    )

    enriched_routes = []
    total_distance = 0.0
    total_duration = 0.0

    for vr in result.routes:
        at_risk_count = sum(1 for slack in (vr.slack_min or []) if slack < AT_RISK_SLACK_THRESHOLD_MIN)
        total_distance += vr.total_distance_km
        total_duration += vr.total_duration_min

        memory_tables.optimization_run_routes.create(
            run_id=run["id"],
            vehicle_id=int(vr.vehicle_id),
            distance=vr.total_distance_km,
            duration=vr.total_duration_min,
            risk=at_risk_count,
        )

        stops_for_response = []
        for i, order_id in enumerate(vr.stop_sequence):
            shipment = shipment_by_id.get(str(order_id))
            arrival_min = vr.arrival_min[i] if vr.arrival_min and i < len(vr.arrival_min) else None
            slack_min = vr.slack_min[i] if vr.slack_min and i < len(vr.slack_min) else None
            eta = eta_from_arrival_min(arrival_min) if arrival_min is not None else None

            memory_tables.optimization_run_stops.create(
                run_id=run["id"],
                vehicle_id=int(vr.vehicle_id),
                order_id=int(order_id),
                stop_sequence=i + 1,
                location_name=shipment.location_name if shipment else f"Order {order_id}",
                lat=shipment.lat if shipment else None,
                lng=shipment.lng if shipment else None,
                eta=eta,
                slack_min=slack_min,
            )
            stops_for_response.append(
                {
                    "order_id": order_id,
                    "location_name": shipment.location_name if shipment else f"Order {order_id}",
                    "lat": shipment.lat if shipment else None,
                    "lng": shipment.lng if shipment else None,
                    "eta": eta.isoformat() if eta else None,
                    "slack_min": slack_min,
                }
            )

        enriched_routes.append(
            {
                "vehicle_id": vr.vehicle_id,
                "at_risk_count": at_risk_count,
                "total_distance_km": vr.total_distance_km,
                "total_duration_min": vr.total_duration_min,
                "stops": stops_for_response,
            }
        )

    unassigned_for_response = [
        {
            "order_id": u.shipment_id,
            "reason": u.reason,
            "detail": u.detail,
            "location_name": shipment_by_id[u.shipment_id].location_name if u.shipment_id in shipment_by_id else None,
        }
        for u in result.unassigned
    ]

    return {
        "feasible": True,
        "runId": run["id"],
        "mode": mode,
        "objective": objective,
        "routes": enriched_routes,
        "unassigned": unassigned_for_response,
        "totalDistanceKm": round(total_distance, 2),
        "totalDurationMin": round(total_duration, 1),
        "warnings": fleet_data.warnings,
        "routing": routing_metadata,
        "returnToWarehouse": fleet_data.return_to_warehouse,
        "returnWarehouse": fleet_data.return_warehouse,
        "avoidTolls": avoid_tolls,
        # Route Optimization does not compute tolls: fleet costs exclude them unless they are avoided.
        "costNote": "Tolls avoided" if avoid_tolls else "tolls not included",
    }


@router.post("/preview")
async def preview_optimization(body: PreviewBody, db: Session = Depends(get_db)):
    mode = "reoptimize" if body.mode == "reoptimize" else "initial"
    return await _run_optimization(db, mode=mode, vehicle_ids=body.vehicleIds, order_ids=body.orderIds, objective=body.objective, return_to_warehouse=body.returnToWarehouse, return_warehouse_id=body.returnWarehouseId, avoid_tolls=body.avoidTolls)


@router.post("/reoptimize")
async def reoptimize(body: ReoptimizeBody, db: Session = Depends(get_db)):
    if not body.reason:
        raise HTTPException(400, 'reason is required (e.g. "vehicle_deviation", "urgent_delivery", "delay").')
    result = await _run_optimization(db, mode="reoptimize", vehicle_ids=body.vehicleIds, order_ids=body.orderIds, objective=body.objective, return_to_warehouse=body.returnToWarehouse, return_warehouse_id=body.returnWarehouseId, avoid_tolls=body.avoidTolls)
    return {**result, "reason": body.reason}


class ApplyBody(BaseModel):
    runId: int


@router.post("/apply")
async def apply_optimization(body: ApplyBody, db: Session = Depends(get_db), current_user: CurrentUser = Depends(require_role("admin", "dispatcher"))):
    run = memory_tables.optimization_runs.get(body.runId)
    if run is None:
        raise HTTPException(404, "Optimization run not found")
    if run["status"] != "proposed":
        raise HTTPException(409, f"Optimization run is '{run['status']}', not 'proposed'. It cannot be applied again.")

    try:
        stored_snapshot = json.loads(run.get("data_snapshot") or "{}")
    except json.JSONDecodeError:
        stored_snapshot = {}
    try:
        scope = json.loads(run.get("scope") or "[]")
    except json.JSONDecodeError:
        scope = []
    if isinstance(scope, dict):
        scope_vehicle_ids = scope.get("vehicle_ids")
        scope_order_ids = scope.get("order_ids")
    else:
        # Compatibility with runs created before scoped order IDs were persisted.
        scope_vehicle_ids = scope
        scope_order_ids = None

    try:
        fresh_data = await fetch_fleet_data(
            db,
            mode=run.get("mode") or "initial",
            vehicle_ids=scope_vehicle_ids,
            order_ids=scope_order_ids,
            return_to_warehouse=bool(stored_snapshot.get("return_to_warehouse")),
            return_warehouse_id=stored_snapshot.get("return_warehouse_id"),
        )
    except FleetDataError as e:
        memory_tables.optimization_runs.update(run["id"], status="failed")
        raise HTTPException(409, detail={"message": f"Optimization is stale and could not be re-validated: {e} Please run optimization again.", "issues": e.issues})

    is_stale = (
        json.dumps(stored_snapshot.get("vehicles") or []) != json.dumps(fresh_data.data_snapshot.get("vehicles") or [])
        or json.dumps(stored_snapshot.get("orders") or []) != json.dumps(fresh_data.data_snapshot.get("orders") or [])
    )
    if is_stale:
        memory_tables.optimization_runs.update(run["id"], status="superseded")
        raise HTTPException(409, "Optimization is stale. Operational data changed after this preview was generated. Please run optimization again.")

    opt_routes = memory_tables.optimization_run_routes.list(run_id=run["id"])
    opt_stops = sorted(memory_tables.optimization_run_stops.list(run_id=run["id"]), key=lambda s: s.get("stop_sequence") or 0)

    applied_routes = []
    for r in opt_routes:
        existing_manifest = db.execute(
            select(LoadManifest).where(LoadManifest.vehicle_id == str(r["vehicle_id"]))
        ).scalars().first()

        if existing_manifest is not None and existing_manifest.route_id:
            route_id = int(existing_manifest.route_id)
            route = memory_tables.routes.update(
                route_id,
                distance_km=float(r["distance"]) if r["distance"] is not None else None,
                duration_min=float(r["duration"]) if r["duration"] is not None else None,
                status="applied",
            )
            for stop in memory_tables.route_stops.list(route_id=route_id):
                memory_tables.route_stops.delete(stop["id"])
        else:
            route = memory_tables.routes.create(
                name=f"Route for vehicle {r['vehicle_id']}",
                mode=run.get("objective") or "recommended",
                distance_km=float(r["distance"]) if r["distance"] is not None else None,
                duration_min=float(r["duration"]) if r["duration"] is not None else None,
                status="applied",
            )
            route_id = route["id"]
            db.add(LoadManifest(route_id=str(route_id), vehicle_id=str(r["vehicle_id"]), status="draft"))
            db.flush()

        v_stops = sorted(
            (s for s in opt_stops if s["vehicle_id"] == r["vehicle_id"]),
            key=lambda s: s.get("stop_sequence") or 0,
        )

        ordered_coords: list[list[float]] = []
        for s in v_stops:
            lat = float(s["lat"]) if s["lat"] is not None else 0.0
            lng = float(s["lng"]) if s["lng"] is not None else 0.0
            memory_tables.route_stops.create(
                route_id=route_id,
                sequence=s["stop_sequence"],
                location_name=s["location_name"] or f"Stop {s['stop_sequence']}",
                lat=lat,
                lng=lng,
            )
            if s["lat"] is not None and s["lng"] is not None:
                ordered_coords.append([lng, lat])
            if s["order_id"]:
                order = memory_tables.orders.get(int(s["order_id"]))
                if order is not None:
                    memory_tables.orders.update(order["id"], route_id=str(route_id), vehicle_id=str(r["vehicle_id"]))

        polyline_warning = None
        if len(ordered_coords) >= 2:
            polyline_result = await fetch_route_polyline(ordered_coords, avoid_tolls=bool(stored_snapshot.get("avoid_tolls")))
            if polyline_result["polyline"]:
                memory_tables.routes.update(route_id, polyline_geojson=polyline_result["polyline"])
            else:
                polyline_warning = polyline_result["skippedReason"]

        applied_routes.append({"routeId": route_id, "vehicleId": r["vehicle_id"], "polylineWarning": polyline_warning})

    memory_tables.optimization_runs.update(run["id"], status="applied", applied_at=datetime.now(timezone.utc))
    db.flush()

    write_audit_log(db, actor_id=str(current_user.id), action="apply_optimization", target_entity="optimization_run", target_id=run["id"], details={
        "route_ids": [r["routeId"] for r in applied_routes],
        "vehicle_ids": [r["vehicleId"] for r in applied_routes],
        "objective": run.get("objective"),
        "mode": run.get("mode"),
        "provider": "mapbox" if os.environ.get("MAPBOX_SERVER_TOKEN") else "fallback",
        "profile": os.environ.get("MAPBOX_PROFILE", "driving-traffic"),
        "traffic_aware": bool(os.environ.get("MAPBOX_SERVER_TOKEN")),
    })
    return {"success": True, "routes": applied_routes}


@router.get("/{run_id}")
def get_optimization(run_id: int):
    run = memory_tables.optimization_runs.get(run_id)
    if run is None:
        raise HTTPException(404, "Optimization run not found")

    routes = memory_tables.optimization_run_routes.list(run_id=run_id)
    stops = sorted(memory_tables.optimization_run_stops.list(run_id=run_id), key=lambda s: s.get("stop_sequence") or 0)

    routes_with_stops = []
    for r in routes:
        r_dict = dict(r)
        r_dict["stops"] = [dict(s) for s in stops if s["vehicle_id"] == r["vehicle_id"]]
        routes_with_stops.append(r_dict)

    return {**run, "routes": routes_with_stops}
