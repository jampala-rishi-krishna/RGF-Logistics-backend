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
from services.route_costs import DEFAULT_SERVICE_MIN_PER_STOP, compute_route_cost_breakdown, describe_rates, load_route_cost_config
from services.serialize import row_to_dict
from services.time_utils import minutes_since_midnight
from services.warehouses import ReturnWarehouseRequired, list_warehouses, resolve_return_warehouse

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
    returnToWarehouse: bool | None = None
    returnWarehouseId: str | None = None


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
    returnToWarehouse: bool = False
    returnWarehouseId: str | None = None
    optimizeStopOrder: bool = False
    hasHelper: bool = False
    # True for a reefer truck; False -> refrigeration cost is 0.
    refrigerated: bool = True
    # "frozen" if any order on the route is frozen cold chain; None/"chilled" -> chilled (None = "assumed chilled").
    coldChainCategory: str | None = None


class OptimizeStopsBody(BaseModel):
    origin: dict
    destination: dict
    stops: list[dict]
    mode: str = "fastest"
    returnToWarehouse: bool = False
    returnWarehouseId: str | None = None
    hasHelper: bool = False
    refrigerated: bool = True
    coldChainCategory: str | None = None


@router.get("/routes/warehouses")
def route_warehouses():
    return {"warehouses": list_warehouses()}


def _leg_part(distance_km: float, duration_min: float, cost_config: dict, *, refrigeration_hours: float, has_helper: bool, frozen: bool, refrigerated: bool) -> dict:
    return {
        "distanceKm": round(float(distance_km), 2),
        "durationMin": round(float(duration_min), 1),
        "costBreakdown": compute_route_cost_breakdown(
            float(distance_km),
            float(duration_min),
            config=cost_config,
            has_helper=has_helper,
            refrigeration_hours=refrigeration_hours,
            frozen=frozen,
            refrigerated=refrigerated,
        ),
    }


def _split_round_trip(
    legs: list[dict],
    return_to_warehouse: bool,
    cost_config: dict,
    *,
    service_min: float = 0.0,
    has_helper: bool = False,
    frozen: bool = False,
    refrigerated: bool = True,
) -> dict:
    """Split Google's legs into Outbound (origin -> last delivery) and Return (last delivery ->
    warehouse) and cost each. TOTAL is the component-wise sum of the rows, so the row totals add
    up to it exactly. Refrigeration runs for driving + service time outbound; on the return leg
    only when ROUTE_REFRIGERATION_ON_RETURN_LEG is on."""
    if return_to_warehouse and legs:
        outbound_legs, return_legs = legs[:-1], legs[-1:]
    else:
        outbound_legs, return_legs = legs, []
    outbound_distance = sum(float(leg.get("distance_km") or 0) for leg in outbound_legs)
    outbound_duration = sum(float(leg.get("duration_min") or 0) for leg in outbound_legs)
    return_distance = sum(float(leg.get("distance_km") or 0) for leg in return_legs)
    return_duration = sum(float(leg.get("duration_min") or 0) for leg in return_legs)
    common = dict(has_helper=has_helper, frozen=frozen, refrigerated=refrigerated)
    outbound = _leg_part(outbound_distance, outbound_duration, cost_config, refrigeration_hours=(outbound_duration + service_min) / 60.0, **common)
    return_leg = None
    if return_to_warehouse:
        return_leg = _leg_part(
            return_distance, return_duration, cost_config,
            refrigeration_hours=(return_duration / 60.0) if cost_config.get("refrigeration_on_return_leg") else 0.0,
            **common,
        )
    parts = [outbound] + ([return_leg] if return_leg else [])
    total = {
        "distanceKm": round(sum(p["distanceKm"] for p in parts), 2),
        "durationMin": round(sum(p["durationMin"] for p in parts), 1),
        "costBreakdown": {key: round(sum(p["costBreakdown"][key] for p in parts), 2) for key in ("distance", "time", "fuel", "refrigeration", "total")},
    }
    return {"outbound": outbound, "return": return_leg, "total": total}


async def _resolve_point(label: str | None, lat: float | None, lng: float | None, what: str) -> dict:
    if lat is not None and lng is not None:
        return {"lat": float(lat), "lng": float(lng)}
    resolved = await geocode_address(label)
    if resolved is None:
        raise HTTPException(404, f"{what} could not be found")
    return resolved


@router.post("/routes/plan")
async def plan_route(body: RoutePlanBody):
    """Geocode the user's locations and calculate a real road route and round-trip cost estimate.

    Routes API request: origin = start; intermediates = delivery stops; destination = the chosen
    return warehouse when checked, otherwise the last stop. With optimizeStopOrder, Google reorders
    the intermediates while the destination stays fixed."""
    if not body.origin.strip() or not body.destination.strip():
        raise HTTPException(400, "Origin and destination are required")
    try:
        return_warehouse = resolve_return_warehouse(body.returnToWarehouse, body.returnWarehouseId)
    except ReturnWarehouseRequired as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(400, "Unknown return warehouse.")
    try:
        origin = await _resolve_point(body.origin, body.originLat, body.originLng, "Origin")
        destination = await _resolve_point(body.destination, body.destinationLat, body.destinationLng, "Destination")
        resolved_stops = []
        for stop in body.stops:
            point = await _resolve_point(stop.get("label"), stop.get("lat"), stop.get("lng"), f"Stop {stop.get('label', 'Unnamed stop')}")
            resolved_stops.append({"label": stop.get("label") or "Stop", **point})
        deliveries = [*resolved_stops, {"label": body.destination, **destination}]
        # Round trip: every delivery is an intermediate and the warehouse is the destination.
        intermediates = deliveries if return_warehouse else resolved_stops
        final_point = return_warehouse if return_warehouse else destination
        coordinates = [[origin["lng"], origin["lat"]], *[[p["lng"], p["lat"]] for p in intermediates], [final_point["lng"], final_point["lat"]]]
        ferry_issue = await route_requires_ferry(coordinates)
        if ferry_issue:
            raise HTTPException(422, ferry_issue)
        matrix = await fetch_route_matrix(coordinates)
        route_geometry = await fetch_route_polyline(coordinates, optimize_waypoint_order=body.optimizeStopOrder)
    except OrsError as e:
        raise HTTPException(502, str(e))

    optimized_order = route_geometry.get("optimized_waypoint_order") or []
    if body.optimizeStopOrder and len(optimized_order) == len(intermediates):
        intermediates = [intermediates[i] for i in optimized_order]
        deliveries = intermediates if return_warehouse else [*intermediates, {"label": body.destination, **destination}]
        if not return_warehouse:
            resolved_stops = intermediates
    if return_warehouse:
        resolved_stops, last_delivery = deliveries[:-1], deliveries[-1]
        destination = {"lat": last_delivery["lat"], "lng": last_delivery["lng"]}
        destination_label = last_delivery.get("label") or body.destination
    else:
        destination_label = body.destination

    cost_config = load_route_cost_config()
    legs = route_geometry.get("legs") or []
    if not legs:
        legs = [{"distance_km": float(route_geometry.get("distance_km") or matrix["distance_matrix_km"][0][-1]), "duration_min": float(route_geometry.get("duration_min") or matrix["duration_matrix_min"][0][-1])}]
    frozen = (body.coldChainCategory or "").strip().lower() == "frozen"
    round_trip = _split_round_trip(
        legs, bool(return_warehouse), cost_config,
        service_min=DEFAULT_SERVICE_MIN_PER_STOP * len(deliveries),
        has_helper=body.hasHelper, frozen=frozen, refrigerated=body.refrigerated,
    )
    distance_km = round_trip["total"]["distanceKm"]
    duration_min = round_trip["total"]["durationMin"]
    cost_breakdown = round_trip["total"]["costBreakdown"]
    cost = cost_breakdown["total"]
    if body.mode == "cheapest":
        objective_note = "Lowest estimated operating cost, based on configured fuel, distance, driver time, and refrigeration rates."
    elif body.mode == "shortest":
        objective_note = "Shortest road distance, with time kept secondary."
    elif body.mode == "balanced":
        objective_note = "Balanced road route using normalized time, distance, and operating cost."
    else:
        objective_note = "Fastest road travel time."
    return {
        "origin": {"label": body.origin, **origin},
        "destination": {"label": destination_label, **destination},
        "stops": resolved_stops,
        "returnToWarehouse": bool(return_warehouse),
        "returnWarehouse": return_warehouse,
        "mode": body.mode,
        "objectiveNote": objective_note,
        "distanceKm": distance_km,
        "durationMin": duration_min,
        "cost": cost,
        "costBreakdown": cost_breakdown,
        "roundTrip": round_trip,
        "costAssumptions": {
            "hasHelper": body.hasHelper,
            "refrigerated": body.refrigerated,
            "coldChain": "frozen" if frozen else "chilled",
            "coldChainAssumed": not body.coldChainCategory,
            "serviceMinPerStop": DEFAULT_SERVICE_MIN_PER_STOP,
        },
        "rates": describe_rates(cost_config),
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
    """Let Google's Routes API reorder the stops (optimizeWaypointOrder) with the destination fixed:
    the chosen warehouse on a round trip, else the last stop."""
    if len(body.stops) < 2:
        raise HTTPException(400, "At least two intermediate stops are required to optimize their order.")
    if any(p.get("lat") is None or p.get("lng") is None for p in (body.origin, body.destination, *body.stops)):
        raise HTTPException(400, "Every origin, stop, and destination must have valid coordinates.")
    result = await plan_route(RoutePlanBody(
        origin=str(body.origin.get("label") or "Origin"),
        destination=str(body.destination.get("label") or "Destination"),
        originLat=float(body.origin["lat"]), originLng=float(body.origin["lng"]),
        destinationLat=float(body.destination["lat"]), destinationLng=float(body.destination["lng"]),
        stops=body.stops, mode=body.mode,
        returnToWarehouse=body.returnToWarehouse,
        returnWarehouseId=body.returnWarehouseId,
        optimizeStopOrder=True,
        hasHelper=body.hasHelper, refrigerated=body.refrigerated, coldChainCategory=body.coldChainCategory,
    ))
    result["optimizedStopOrder"] = [stop.get("label") for stop in result["stops"]]
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
        return_to_warehouse=bool(body.returnToWarehouse),
        return_warehouse_id=body.returnWarehouseId,
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
