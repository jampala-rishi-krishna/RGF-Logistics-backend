from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from auth.dependencies import CurrentUser, require_role
from models.route import LoadManifest, ManifestItem
from models.vehicle import Vehicle
from services import live_gps_store, memory_tables, optimizer
from services.google_maps import OrsError, fetch_route_matrix, fetch_route_polyline, geocode_address, route_requires_ferry, search_addresses
from services.route_costs import build_objective_cost_matrix, compute_route_cost_breakdown, load_route_cost_config
from services.serialize import row_to_dict
from services.time_utils import minutes_since_midnight

router = APIRouter(tags=["routes"], dependencies=[Depends(require_role("admin", "dispatcher"))])

# routes-module/index.js enforces no role gate and writes no audit_log entries on any of these
# routes - ported faithfully (confirmed pre-existing gap, not newly introduced here).


@router.get("/routes")
def list_routes():
    return memory_tables.routes.list()


class CreateRouteBody(BaseModel):
    name: str
    mode: str
    distanceKm: float | None = None
    durationMin: float | None = None
    cost: float | None = None
    status: str | None = None


@router.get("/routes/geocode")
async def geocode_route_location(text: str = Query(min_length=2)):
    """Resolve a Philippine origin/destination through the configured geocoder."""
    try:
        result = await geocode_address(text)
    except OrsError as e:
        raise HTTPException(502, str(e))
    if result is None:
        raise HTTPException(404, "Location could not be found")
    return {"query": text, **result}


@router.get("/routes/search")
async def search_route_locations(text: str = Query(min_length=2)):
    return await search_addresses(text)


class RoutePlanBody(BaseModel):
    origin: str
    destination: str
    mode: str = "fastest"
    originLat: float | None = None
    originLng: float | None = None
    destinationLat: float | None = None
    destinationLng: float | None = None
    stops: list[dict] = []


class OptimizeStopsBody(BaseModel):
    origin: dict
    destination: dict
    stops: list[dict]
    mode: str = "fastest"


@router.post("/routes/plan")
async def plan_route(body: RoutePlanBody):
    """Geocode two user locations and calculate a real road route and cost estimate."""
    if not body.origin.strip() or not body.destination.strip():
        raise HTTPException(400, "Origin and destination are required")
    try:
        origin, destination = await __import__("asyncio").gather(
            geocode_address(body.origin) if body.originLat is None or body.originLng is None else __import__("asyncio").sleep(0, result={"lat": body.originLat, "lng": body.originLng}),
            geocode_address(body.destination) if body.destinationLat is None or body.destinationLng is None else __import__("asyncio").sleep(0, result={"lat": body.destinationLat, "lng": body.destinationLng}),
        )
        if origin is None or destination is None:
            raise HTTPException(404, "Origin or destination could not be found")
        resolved_stops = []
        for stop in body.stops:
            if stop.get("lat") is not None and stop.get("lng") is not None:
                resolved_stops.append({"label": stop.get("label") or "Stop", "lat": float(stop["lat"]), "lng": float(stop["lng"])})
            else:
                resolved = await geocode_address(stop.get("label"))
                if resolved is None:
                    raise HTTPException(404, f"Stop could not be found: {stop.get('label', 'Unnamed stop')}")
                resolved_stops.append({"label": stop.get("label") or "Stop", **resolved})
        points = [origin, *resolved_stops, destination]
        coordinates = [[point["lng"], point["lat"]] for point in points]
        ferry_issue = await route_requires_ferry(coordinates)
        if ferry_issue:
            raise HTTPException(422, ferry_issue)
        matrix = await fetch_route_matrix(coordinates)
        route_geometry = await fetch_route_polyline(
            coordinates
        )
    except OrsError as e:
        raise HTTPException(502, str(e))

    distance_km = round(float(route_geometry.get("distance_km") or matrix["distance_matrix_km"][0][-1]), 2)
    duration_min = round(float(route_geometry.get("duration_min") or matrix["duration_matrix_min"][0][-1]), 1)
    cost_config = load_route_cost_config()
    cost_breakdown = compute_route_cost_breakdown(distance_km, duration_min, config=cost_config)
    cost = cost_breakdown["total"]
    if body.mode == "cheapest":
        objective_note = "Lowest estimated operating cost, based on configured distance, time, fuel, and refrigeration rates."
    elif body.mode == "shortest":
        objective_note = "Shortest road distance, with time kept secondary."
    elif body.mode == "balanced":
        objective_note = "Balanced road route using normalized time, distance, and operating cost."
    else:
        objective_note = "Fastest road travel time."
    return {
        "origin": {"label": body.origin, **origin},
        "destination": {"label": body.destination, **destination},
        "stops": resolved_stops,
        "mode": body.mode,
        "objectiveNote": objective_note,
        "distanceKm": distance_km,
        "durationMin": duration_min,
        "cost": cost,
        "costBreakdown": cost_breakdown,
        "routing": {
            "provider": matrix.get("provider"),
            "profile": matrix.get("profile"),
            "trafficAware": bool(matrix.get("traffic_aware")),
            "geometryProvider": route_geometry.get("provider"),
            "geometryProfile": route_geometry.get("profile"),
            "geometryTrafficAware": bool(route_geometry.get("traffic_aware")),
            "calculatedAt": matrix.get("calculated_at"),
            "departureTime": matrix.get("departure_time"),
            "fallback": bool(matrix.get("fallback_used") or route_geometry.get("fallback_used")),
            "fallbackUsed": bool(matrix.get("fallback_used") or route_geometry.get("fallback_used")),
            "fallbackReason": matrix.get("fallback_reason") or route_geometry.get("fallback_reason"),
        },
        "geometry": route_geometry["polyline"],
        "warnings": ([route_geometry["skippedReason"]] if route_geometry["skippedReason"] else []) + (["Distance and geometry were returned by different providers."] if matrix.get("provider") != route_geometry.get("provider") else []),
    }


@router.post("/routes/optimize-stops")
async def optimize_stop_order(body: OptimizeStopsBody):
    """Find an ordered stop sequence from a real road matrix, then build its geometry."""
    if len(body.stops) < 2:
        raise HTTPException(400, "At least two intermediate stops are required to optimize their order.")
    points = [body.origin, *body.stops, body.destination]
    if any(p.get("lat") is None or p.get("lng") is None for p in points):
        raise HTTPException(400, "Every origin, stop, and destination must have valid coordinates.")
    coordinates = [[float(p["lng"]), float(p["lat"])] for p in points]
    matrix = await fetch_route_matrix(coordinates)
    unvisited = set(range(1, len(points) - 1))
    order = []
    current = 0
    objective_matrix = build_objective_cost_matrix(
        matrix["distance_matrix_km"],
        matrix["duration_matrix_min"],
        objective=body.mode,
        balanced_time_weight=float(os.environ.get("ROUTE_BALANCED_TIME_WEIGHT", "0.5")),
        balanced_distance_weight=float(os.environ.get("ROUTE_BALANCED_DISTANCE_WEIGHT", "0.2")),
        balanced_cost_weight=float(os.environ.get("ROUTE_BALANCED_COST_WEIGHT", "0.3")),
    )
    while unvisited:
        next_stop = min(unvisited, key=lambda index: objective_matrix[current][index])
        order.append(next_stop)
        unvisited.remove(next_stop)
        current = next_stop
    reordered = [body.stops[index - 1] for index in order]
    result = await plan_route(RoutePlanBody(
        origin=str(body.origin.get("label") or "Origin"),
        destination=str(body.destination.get("label") or "Destination"),
        originLat=float(body.origin["lat"]), originLng=float(body.origin["lng"]),
        destinationLat=float(body.destination["lat"]), destinationLng=float(body.destination["lng"]),
        stops=reordered, mode=body.mode,
    ))
    result["optimizedStopOrder"] = [stop.get("label") for stop in reordered]
    return result


@router.post("/routes", status_code=201)
def create_route(body: CreateRouteBody):
    return memory_tables.routes.create(
        name=body.name,
        mode=body.mode,
        distance_km=body.distanceKm if isinstance(body.distanceKm, (int, float)) else 0,
        duration_min=body.durationMin if isinstance(body.durationMin, (int, float)) else 0,
        cost=body.cost if isinstance(body.cost, (int, float)) else 0,
        status=body.status or "draft",
    )


@router.get("/routes/{route_id}/stops")
def list_route_stops(route_id: int):
    if memory_tables.routes.get(route_id) is None:
        raise HTTPException(404, "Route not found")
    return sorted(memory_tables.route_stops.list(route_id=route_id), key=lambda s: s.get("sequence") or 0)


class CreateStopBody(BaseModel):
    sequence: int
    locationName: str
    lat: float | None = None
    lng: float | None = None
    arrivalWindowStart: str | None = None
    arrivalWindowEnd: str | None = None


@router.post("/routes/{route_id}/stops", status_code=201)
def create_route_stop(route_id: int, body: CreateStopBody):
    if memory_tables.routes.get(route_id) is None:
        raise HTTPException(404, "Route not found")
    return memory_tables.route_stops.create(
        route_id=route_id,
        sequence=body.sequence,
        location_name=body.locationName,
        lat=body.lat if isinstance(body.lat, (int, float)) else 0,
        lng=body.lng if isinstance(body.lng, (int, float)) else 0,
        arrival_window_start=body.arrivalWindowStart or None,
        arrival_window_end=body.arrivalWindowEnd or None,
    )


@router.get("/manifests")
def list_manifests(db: Session = Depends(get_db)):
    return [row_to_dict(m) for m in db.execute(select(LoadManifest)).scalars().all()]


class CreateManifestBody(BaseModel):
    routeId: str | None = None
    vehicleId: str | None = None
    status: str | None = None
    cargoType: str | None = None


@router.post("/manifests", status_code=201)
def create_manifest(body: CreateManifestBody, db: Session = Depends(get_db)):
    manifest = LoadManifest(
        route_id=str(body.routeId) if body.routeId else "",
        vehicle_id=str(body.vehicleId) if body.vehicleId else "",
        status=body.status or "draft",
        cargo_type=body.cargoType or "",
    )
    db.add(manifest)
    db.commit()
    db.refresh(manifest)
    return row_to_dict(manifest)


@router.get("/manifests/{manifest_id}/items")
def list_manifest_items(manifest_id: int, db: Session = Depends(get_db)):
    if db.get(LoadManifest, manifest_id) is None:
        raise HTTPException(404, "Manifest not found")
    stmt = select(ManifestItem).where(ManifestItem.manifest_id == str(manifest_id))
    return [row_to_dict(i) for i in db.execute(stmt).scalars().all()]


class CreateManifestItemBody(BaseModel):
    cargoCategory: str
    quantity: int
    tempRequirementC: float | None = None


@router.post("/manifests/{manifest_id}/items", status_code=201)
def create_manifest_item(manifest_id: int, body: CreateManifestItemBody, db: Session = Depends(get_db)):
    if db.get(LoadManifest, manifest_id) is None:
        raise HTTPException(404, "Manifest not found")
    item = ManifestItem(
        manifest_id=str(manifest_id),
        cargo_category=body.cargoCategory,
        quantity=body.quantity,
        temp_requirement_c=body.tempRequirementC if isinstance(body.tempRequirementC, (int, float)) else 0,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return row_to_dict(item)


class OptimizeRouteBody(BaseModel):
    routeId: int


@router.post("/routes/optimize")
async def optimize_single_route(body: OptimizeRouteBody, db: Session = Depends(get_db)):
    route = memory_tables.routes.get(body.routeId)
    if route is None:
        raise HTTPException(404, "Route not found")

    stops = sorted(memory_tables.route_stops.list(route_id=body.routeId), key=lambda s: s.get("sequence") or 0)
    if not stops:
        raise HTTPException(400, "Route has no stops to optimize")
    missing_coords = [s for s in stops if s["lat"] is None or s["lng"] is None]
    if missing_coords:
        raise HTTPException(422, f"{len(missing_coords)} stops require valid coordinates.")

    manifest = db.execute(
        select(LoadManifest).where(LoadManifest.route_id == str(body.routeId))
    ).scalars().first()
    if manifest is None or not manifest.vehicle_id:
        raise HTTPException(400, "No vehicle assigned to this route via load_manifests - cannot optimize without a start point")

    vehicle = db.get(Vehicle, int(manifest.vehicle_id))
    if vehicle is None:
        raise HTTPException(404, "Assigned vehicle not found")
    live_position = live_gps_store.get(str(vehicle.plate_no or "").strip().upper())
    if live_position is None:
        raise HTTPException(422, "Assigned vehicle is missing a current Cartrack GPS position.")

    if vehicle.capacity_kg is None:
        raise HTTPException(422, f"Vehicle {vehicle.id} has no capacity_kg configured.")

    locations = [[live_position["lng"], live_position["lat"]]] + [[s["lng"], s["lat"]] for s in stops]
    ferry_issue = await route_requires_ferry(locations)
    if ferry_issue:
        raise HTTPException(422, ferry_issue)

    try:
        matrix = await fetch_route_matrix(locations)
    except OrsError as e:
        raise HTTPException(502, f"Road travel data unavailable. Optimization could not be completed. ({e})")

    objective = route["mode"] if route["mode"] in ("fastest", "cheapest", "shortest", "balanced") else "fastest"
    opt_request = optimizer.OptimizeRequest(
        vehicles=[
            optimizer.Vehicle(
                id=str(vehicle.id),
                capacity_kg=float(vehicle.capacity_kg),
                start_node=0,
                end_node=len(stops),
                shift_start=minutes_since_midnight(vehicle.shift_start) or 0,
                shift_end=minutes_since_midnight(vehicle.shift_end) or 1440,
                temperature_capabilities=(
                    vehicle.temperature_capability.split(",") if vehicle.temperature_capability else ["ambient"]
                ),
            )
        ],
        shipments=[
            optimizer.Shipment(
                id=str(s["id"]),
                node=i + 1,
                demand_kg=0,
                service_time_min=0,
                time_window_start=minutes_since_midnight(s.get("arrival_window_start")) or 0,
                time_window_end=minutes_since_midnight(s.get("arrival_window_end")) or 1440,
                temperature_requirement="ambient",
                priority=1,
            )
            for i, s in enumerate(stops)
        ],
        distance_matrix_km=matrix["distance_matrix_km"],
        duration_matrix_min=matrix["duration_matrix_min"],
        objective=objective,
    )
    result = optimizer.solve(opt_request)

    if not result.feasible:
        return {"feasible": False, "message": result.message}

    vr = result.routes[0]
    stops_by_id = {str(s["id"]): s for s in stops}
    for i, stop_id in enumerate(vr.stop_sequence):
        stop = stops_by_id.get(str(stop_id))
        if stop:
            memory_tables.route_stops.update(stop["id"], sequence=i + 1)
    route = memory_tables.routes.update(body.routeId, distance_km=vr.total_distance_km, duration_min=vr.total_duration_min)

    warnings: list[str] = []
    ordered_coords = [[live_position["lng"], live_position["lat"]]]
    for stop_id in vr.stop_sequence:
        stop = stops_by_id.get(str(stop_id))
        if stop:
            ordered_coords.append([stop["lng"], stop["lat"]])
    polyline_result = await fetch_route_polyline(ordered_coords)
    if polyline_result["polyline"]:
        route = memory_tables.routes.update(body.routeId, polyline_geojson=polyline_result["polyline"])
    else:
        warnings.append(polyline_result["skippedReason"])

    return {
        "feasible": True,
        "route": route,
        "stopSequence": vr.stop_sequence,
        "totalDistanceKm": vr.total_distance_km,
        "totalDurationMin": vr.total_duration_min,
        "warnings": warnings,
    }
