from __future__ import annotations

import os
from typing import TypedDict


class RouteCostConfig(TypedDict):
    distance_rate_per_km: float
    distance_rate_configured: bool
    time_rate_per_hour: float
    time_rate_configured: bool
    fuel_surcharge_per_km: float
    fuel_surcharge_configured: bool
    refrigeration_cost_per_hour: float
    refrigeration_cost_configured: bool
    fixed_cost_per_route: float
    fixed_cost_configured: bool


def _read_float_env(name: str, default: float) -> tuple[float, bool]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default, False
    try:
        return float(raw), True
    except ValueError:
        return default, False


def load_route_cost_config() -> RouteCostConfig:
    distance_rate_per_km, distance_rate_configured = _read_float_env("ROUTE_DISTANCE_COST_PER_KM", 32.0)
    time_rate_per_hour, time_rate_configured = _read_float_env("ROUTE_DRIVER_COST_PER_HOUR", 180.0)
    fuel_surcharge_per_km, fuel_surcharge_configured = _read_float_env("ROUTE_FUEL_COST_PER_KM", 0.0)
    refrigeration_cost_per_hour, refrigeration_cost_configured = _read_float_env("ROUTE_REFRIGERATION_COST_PER_HOUR", 0.0)
    fixed_cost_per_route, fixed_cost_configured = _read_float_env("ROUTE_FIXED_COST_PER_ROUTE", 0.0)
    return {
        "distance_rate_per_km": distance_rate_per_km,
        "distance_rate_configured": distance_rate_configured,
        "time_rate_per_hour": time_rate_per_hour,
        "time_rate_configured": time_rate_configured,
        "fuel_surcharge_per_km": fuel_surcharge_per_km,
        "fuel_surcharge_configured": fuel_surcharge_configured,
        "refrigeration_cost_per_hour": refrigeration_cost_per_hour,
        "refrigeration_cost_configured": refrigeration_cost_configured,
        "fixed_cost_per_route": fixed_cost_per_route,
        "fixed_cost_configured": fixed_cost_configured,
    }


def compute_route_cost_breakdown(
    distance_km: float,
    duration_min: float,
    *,
    config: RouteCostConfig | None = None,
    distance_rate_per_km: float | None = None,
    time_rate_per_hour: float | None = None,
    fuel_surcharge_per_km: float | None = None,
    refrigeration_cost_per_hour: float | None = None,
    fixed_cost_per_route: float | None = None,
) -> dict:
    rates = config or load_route_cost_config()
    distance_rate = float(distance_rate_per_km if distance_rate_per_km is not None else rates["distance_rate_per_km"])
    time_rate = float(time_rate_per_hour if time_rate_per_hour is not None else rates["time_rate_per_hour"])
    fuel_rate = float(fuel_surcharge_per_km if fuel_surcharge_per_km is not None else rates["fuel_surcharge_per_km"])
    refrigeration_rate = float(
        refrigeration_cost_per_hour if refrigeration_cost_per_hour is not None else rates["refrigeration_cost_per_hour"]
    )
    fixed_cost = float(fixed_cost_per_route if fixed_cost_per_route is not None else rates["fixed_cost_per_route"])

    distance_cost = float(distance_km) * distance_rate
    time_cost = (float(duration_min) / 60.0) * time_rate
    fuel_cost = float(distance_km) * fuel_rate
    refrigeration_cost = (float(duration_min) / 60.0) * refrigeration_rate
    total = distance_cost + time_cost + fuel_cost + refrigeration_cost + fixed_cost

    return {
        "distance": round(distance_cost, 2),
        "time": round(time_cost, 2),
        "fuel": round(fuel_cost, 2),
        "fuelConfigured": bool(rates["fuel_surcharge_configured"]),
        "refrigeration": round(refrigeration_cost, 2),
        "refrigerationConfigured": bool(rates["refrigeration_cost_configured"]),
        "fixed": round(fixed_cost, 2),
        "fixedConfigured": bool(rates["fixed_cost_configured"]),
        "total": round(total, 2),
    }


def build_objective_cost_matrix(
    distance_km: list[list[float]],
    duration_min: list[list[float]],
    *,
    objective: str,
    config: RouteCostConfig | None = None,
    distance_rate_per_km: float | None = None,
    time_rate_per_hour: float | None = None,
    fuel_surcharge_per_km: float | None = None,
    refrigeration_cost_per_hour: float | None = None,
    balanced_time_weight: float = 0.4,
    balanced_distance_weight: float = 0.2,
    balanced_cost_weight: float = 0.4,
) -> list[list[float]]:
    rates = config or load_route_cost_config()
    distance_rate = float(distance_rate_per_km if distance_rate_per_km is not None else rates["distance_rate_per_km"])
    time_rate = float(time_rate_per_hour if time_rate_per_hour is not None else rates["time_rate_per_hour"])
    fuel_rate = float(fuel_surcharge_per_km if fuel_surcharge_per_km is not None else rates["fuel_surcharge_per_km"])
    refrigeration_rate = float(
        refrigeration_cost_per_hour if refrigeration_cost_per_hour is not None else rates["refrigeration_cost_per_hour"]
    )
    route_distance_rate = distance_rate + fuel_rate
    route_time_rate = time_rate + refrigeration_rate
    n = len(distance_km)
    operating_cost = [
        [
            distance_km[i][j] * route_distance_rate + (duration_min[i][j] / 60.0) * route_time_rate
            for j in range(n)
        ]
        for i in range(n)
    ]
    if objective == "shortest":
        return distance_km
    if objective == "cheapest":
        return operating_cost
    if objective == "balanced":
        max_distance = max((max(row) for row in distance_km), default=1.0) or 1.0
        max_duration = max((max(row) for row in duration_min), default=1.0) or 1.0
        max_cost = max((max(row) for row in operating_cost), default=1.0) or 1.0
        return [
            [
                (duration_min[i][j] / max_duration) * balanced_time_weight
                + (distance_km[i][j] / max_distance) * balanced_distance_weight
                + (operating_cost[i][j] / max_cost) * balanced_cost_weight
                for j in range(n)
            ]
            for i in range(n)
        ]
    return duration_min
