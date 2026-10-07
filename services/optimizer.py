from __future__ import annotations

import os
from typing import Literal, Optional

from pydantic import BaseModel

from services.route_costs import RouteCostConfig, build_objective_cost_matrix, load_route_cost_config

# CVRPTW solver used by the unified optimization service (same
# tested algorithm) - now an in-process module called directly from routers/optimization.py
# and routers/routes.py instead of a separate HTTP microservice.


class Vehicle(BaseModel):
    id: str
    capacity_kg: float
    start_node: int
    end_node: int
    shift_start: float
    shift_end: float
    temperature_capabilities: list[str]
    cost_per_km: Optional[float] = None
    cost_per_hour: Optional[float] = None


class Shipment(BaseModel):
    id: str
    node: int
    demand_kg: float
    service_time_min: float
    time_window_start: float
    time_window_end: float
    temperature_requirement: str
    priority: int


class OptimizeRequest(BaseModel):
    vehicles: list[Vehicle]
    shipments: list[Shipment]
    distance_matrix_km: list[list[float]]
    duration_matrix_min: list[list[float]]
    objective: Literal["recommended", "fastest", "cheapest", "shortest", "balanced"] = "recommended"


class VehicleRoute(BaseModel):
    vehicle_id: str
    stop_sequence: list[str]
    total_distance_km: float
    total_duration_min: float
    arrival_min: list[float] = []
    slack_min: list[float] = []


class UnassignedShipment(BaseModel):
    shipment_id: str
    reason: Literal["ORDER_TOO_HEAVY", "NO_COMPATIBLE_VEHICLE", "TIME_WINDOW_INFEASIBLE", "NO_REACHABLE_VEHICLE"]
    detail: str


class OptimizeResponse(BaseModel):
    feasible: bool
    routes: list[VehicleRoute]
    unassigned: list[UnassignedShipment] = []
    message: Optional[str] = None


# Large relative to any realistic scaled arc cost (costs are scaled by 1000 above), so the
# solver only drops a shipment via AddDisjunction when actually serving it would violate a
# hard constraint (capacity/time window/temperature) - never merely because it's expensive.
DISJUNCTION_DROP_PENALTY = 100_000_000


def _diagnose_unassigned(shipment: "Shipment", vehicles: list["Vehicle"]) -> UnassignedShipment:
    """Best-effort root-cause classification for a shipment the solver could not fit into any
    route. Runs after solving, purely for dispatcher-facing diagnostics - it does not change
    what the solver decided."""
    if shipment.time_window_end <= shipment.time_window_start:
        return UnassignedShipment(
            shipment_id=shipment.id, reason="TIME_WINDOW_INFEASIBLE",
            detail="This delivery's time window start is not before its end.",
        )

    temp_req = (shipment.temperature_requirement or "ambient").strip()
    if temp_req.lower() == "ambient":
        eligible_vehicles = vehicles
    else:
        eligible_vehicles = [v for v in vehicles if temp_req in v.temperature_capabilities]
        if not eligible_vehicles:
            return UnassignedShipment(
                shipment_id=shipment.id, reason="NO_COMPATIBLE_VEHICLE",
                detail=f"No vehicle in the fleet supports the required temperature ({temp_req}).",
            )

    max_capacity = max((v.capacity_kg for v in eligible_vehicles), default=0)
    if shipment.demand_kg > max_capacity:
        return UnassignedShipment(
            shipment_id=shipment.id, reason="ORDER_TOO_HEAVY",
            detail=f"Demand {shipment.demand_kg}kg exceeds the largest {'temperature-compatible ' if temp_req.lower() != 'ambient' else ''}vehicle capacity ({max_capacity}kg).",
        )

    window_fits_any_shift = any(
        shipment.time_window_start < v.shift_end and shipment.time_window_end > v.shift_start
        for v in eligible_vehicles
    )
    if not window_fits_any_shift:
        return UnassignedShipment(
            shipment_id=shipment.id, reason="TIME_WINDOW_INFEASIBLE",
            detail="This delivery's time window does not overlap with any eligible vehicle's shift.",
        )

    return UnassignedShipment(
        shipment_id=shipment.id, reason="NO_REACHABLE_VEHICLE",
        detail="Could not fit this stop into any route given current capacity, schedule, and travel-time constraints together.",
    )


def compute_cost_matrix(
    *, objective: str, distance_km: list[list[float]], duration_min: list[list[float]], vehicle: "Vehicle",
    configured_distance_rate: float, configured_time_rate: float,
    config: Optional[RouteCostConfig] = None, refrigerated: bool = False, has_helper: bool = False,
    balanced_time_weight: float, balanced_distance_weight: float, balanced_cost_weight: float,
) -> list[list[float]]:
    """The arc-cost matrix the solver actually optimizes against for one vehicle, given the
    selected objective. Pulled out of solve() as a pure function so objective/weight behavior
    can be unit-tested directly without depending on OR-Tools' local-search tie-breaking."""
    rate_km = vehicle.cost_per_km if vehicle.cost_per_km is not None else configured_distance_rate
    rate_hr = vehicle.cost_per_hour if vehicle.cost_per_hour is not None else configured_time_rate
    return build_objective_cost_matrix(
        distance_km,
        duration_min,
        objective=objective,
        config=config,
        distance_rate_per_km=rate_km,
        time_rate_per_hour_override=rate_hr,
        has_helper=has_helper,
        refrigerated=refrigerated,
        balanced_time_weight=balanced_time_weight,
        balanced_distance_weight=balanced_distance_weight,
        balanced_cost_weight=balanced_cost_weight,
    )


def solve(request: OptimizeRequest) -> OptimizeResponse:
    # OR-Tools ships a native Windows DLL. Load it only when optimization is
    # requested so a workstation policy blocking that DLL does not prevent the
    # rest of the API (login, inventory, fleet, etc.) from starting.
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except (ImportError, OSError) as exc:
        return OptimizeResponse(
            feasible=False,
            routes=[],
            message="Route optimization is unavailable on this machine because the OR-Tools native component could not be loaded.",
        )

    vehicles = request.vehicles
    shipments = request.shipments
    num_vehicles = len(vehicles)
    num_shipments = len(shipments)

    if num_vehicles == 0:
        return OptimizeResponse(feasible=False, routes=[], message="At least one vehicle is required.")
    if num_shipments == 0:
        return OptimizeResponse(feasible=False, routes=[], message="At least one shipment is required.")

    distance_km = request.distance_matrix_km
    duration_min = request.duration_matrix_min

    if not distance_km or not duration_min:
        return OptimizeResponse(feasible=False, routes=[], message="Missing travel matrix. Optimization cannot proceed.")

    num_nodes = len(distance_km)

    if len(duration_min) != num_nodes:
        return OptimizeResponse(feasible=False, routes=[], message="Distance and duration matrices must have the same dimensions.")
    for row in distance_km:
        if len(row) != num_nodes:
            return OptimizeResponse(feasible=False, routes=[], message="Distance matrix must be square.")
    for row in duration_min:
        if len(row) != num_nodes:
            return OptimizeResponse(feasible=False, routes=[], message="Duration matrix must be square.")

    for v in vehicles:
        if not (0 <= v.start_node < num_nodes) or not (0 <= v.end_node < num_nodes):
            return OptimizeResponse(
                feasible=False, routes=[],
                message=f"Vehicle {v.id} has start_node/end_node outside the matrix dimensions (0..{num_nodes - 1})."
            )
    for s in shipments:
        if not (0 <= s.node < num_nodes):
            return OptimizeResponse(
                feasible=False, routes=[],
                message=f"Shipment {s.id} has node {s.node} outside the matrix dimensions (0..{num_nodes - 1})."
            )

    route_cost_config = load_route_cost_config()
    configured_distance_rate = route_cost_config["distance_rate_per_km"]
    configured_time_rate = route_cost_config["driver_cost_per_hour"]
    balanced_time_weight = float(os.environ.get("ROUTE_BALANCED_TIME_WEIGHT", "0.5"))
    balanced_distance_weight = float(os.environ.get("ROUTE_BALANCED_DISTANCE_WEIGHT", "0.2"))
    balanced_cost_weight = float(os.environ.get("ROUTE_BALANCED_COST_WEIGHT", "0.3"))

    starts = [v.start_node for v in vehicles]
    ends = [v.end_node for v in vehicles]

    manager = pywrapcp.RoutingIndexManager(num_nodes, num_vehicles, starts, ends)
    routing = pywrapcp.RoutingModel(manager)

    def make_cost_callback(cost_matrix):
        def cost_callback(from_index, to_index):
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            return int(round(cost_matrix[from_node][to_node] * 1000))
        return cost_callback

    for v_idx, v in enumerate(vehicles):
        cost_matrix = compute_cost_matrix(
            objective=request.objective, distance_km=distance_km, duration_min=duration_min, vehicle=v,
            configured_distance_rate=configured_distance_rate, configured_time_rate=configured_time_rate,
            config=route_cost_config,
            refrigerated=any(c.strip().lower() not in ("", "ambient") for c in v.temperature_capabilities),
            balanced_time_weight=balanced_time_weight, balanced_distance_weight=balanced_distance_weight,
            balanced_cost_weight=balanced_cost_weight,
        )
        cb_index = routing.RegisterTransitCallback(make_cost_callback(cost_matrix))
        routing.SetArcCostEvaluatorOfVehicle(cb_index, v_idx)

    # OR-Tools uses integer capacities; scale to grams so decimal kilograms do
    # not get rounded away at the feasibility boundary.
    capacity_scale = 1000
    demands = [0] * num_nodes
    for s in shipments:
        demands[s.node] = int(round(s.demand_kg * capacity_scale))

    def demand_callback(from_index):
        return demands[manager.IndexToNode(from_index)]

    demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
    vehicle_capacities = [int(round(v.capacity_kg * capacity_scale)) for v in vehicles]
    routing.AddDimensionWithVehicleCapacity(demand_callback_index, 0, vehicle_capacities, True, "Capacity")

    service_times = [0] * num_nodes
    for s in shipments:
        service_times[s.node] = s.service_time_min

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return int(round(duration_min[from_node][to_node] + service_times[from_node]))

    time_callback_index = routing.RegisterTransitCallback(time_callback)
    routing.AddDimension(time_callback_index, 1440, 1440, False, "Time")
    time_dimension = routing.GetDimensionOrDie("Time")

    for s in shipments:
        index = manager.NodeToIndex(s.node)
        time_dimension.CumulVar(index).SetRange(int(s.time_window_start), int(s.time_window_end))

    for i, v in enumerate(vehicles):
        start_index = routing.Start(i)
        time_dimension.CumulVar(start_index).SetRange(int(v.shift_start), int(v.shift_end))
        end_index = routing.End(i)
        time_dimension.CumulVar(end_index).SetRange(int(v.shift_start), int(v.shift_end))

    for i, v in enumerate(vehicles):
        for s in shipments:
            if s.temperature_requirement and s.temperature_requirement not in v.temperature_capabilities:
                if s.temperature_requirement.lower() != "ambient":
                    index = manager.NodeToIndex(s.node)
                    routing.VehicleVar(index).RemoveValue(i)

    # Allow the solver to drop a shipment (at a large penalty) instead of declaring the whole
    # problem infeasible when one order can't be served - see _diagnose_unassigned for the
    # dispatcher-facing reason surfaced for anything actually dropped.
    for s in shipments:
        index = manager.NodeToIndex(s.node)
        routing.AddDisjunction([index], DISJUNCTION_DROP_PENALTY)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.time_limit.seconds = 5

    solution = routing.SolveWithParameters(search_parameters)
    if solution is None:
        return OptimizeResponse(feasible=False, routes=[], message="No feasible solution found for the given vehicles/shipments constraints.")

    routes = []
    shipment_by_node = {s.node: s for s in shipments}

    for v in range(num_vehicles):
        index = routing.Start(v)
        sequence = []
        arrival_min = []
        slack_min = []
        total_distance = 0.0
        total_duration = 0.0
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node in shipment_by_node:
                s = shipment_by_node[node]
                sequence.append(s.id)
                arrival = solution.Value(time_dimension.CumulVar(index))
                arrival_min.append(float(arrival))
                slack_min.append(float(s.time_window_end) - float(arrival))
            next_index = solution.Value(routing.NextVar(index))
            next_node = manager.IndexToNode(next_index)
            total_distance += distance_km[node][next_node]
            total_duration += duration_min[node][next_node] + service_times[node]
            index = next_index
        routes.append(
            VehicleRoute(
                vehicle_id=vehicles[v].id,
                stop_sequence=sequence,
                total_distance_km=round(total_distance, 2),
                total_duration_min=round(total_duration, 1),
                arrival_min=arrival_min,
                slack_min=slack_min,
            )
        )

    assigned_ids = {stop_id for r in routes for stop_id in r.stop_sequence}
    unassigned = [_diagnose_unassigned(s, vehicles) for s in shipments if s.id not in assigned_ids]

    message = None
    if unassigned:
        message = f"{len(unassigned)} of {num_shipments} deliveries could not be assigned to any vehicle."

    return OptimizeResponse(feasible=True, routes=routes, unassigned=unassigned, message=message)
