"""Route cost model (all rates come from env; see .env.example).

Per leg:
    fuel          = km / ROUTE_FUEL_KM_PER_LITER * ROUTE_DIESEL_PRICE_PER_LITER      (distance only, counted once)
    distance cost = km * ROUTE_DISTANCE_COST_PER_KM                                   (maintenance/tyres/depreciation)
    time cost     = hours * (ROUTE_DRIVER_COST_PER_HOUR [+ ROUTE_HELPER_COST_PER_HOUR if a helper is assigned])
    refrigeration = refrigeration_hours * litres/hour * ROUTE_DIESEL_PRICE_PER_LITER
    tolls         = Google leg toll estimate (Class 1, PHP) * class multiplier (Class 2 = ROUTE_TOLL_CLASS2_MULTIPLIER, ...)
    leg total     = fuel + distance cost + time cost + refrigeration + tolls (known fees only)

The old ROUTE_FUEL_COST_PER_KM, ROUTE_REFRIGERATION_COST_PER_HOUR, REFRIGERATION_ON_RETURN_LEG and
ROUTE_FIXED_COST_PER_ROUTE names are no longer read; leaving them set on Render is harmless.
"""
from __future__ import annotations

import os
from typing import TypedDict

# Assumed on-site time per delivery stop; refrigeration keeps running while the truck is stopped.
DEFAULT_SERVICE_MIN_PER_STOP = 30.0


class RouteCostConfig(TypedDict):
    distance_rate_per_km: float
    distance_rate_configured: bool
    diesel_price_per_liter: float
    fuel_km_per_liter: float
    fuel_cost_per_km: float
    driver_cost_per_hour: float
    helper_cost_per_hour: float
    refrigeration_liters_per_hour_chilled: float
    refrigeration_liters_per_hour_frozen: float
    refrigeration_on_return_leg: bool
    tolls_enabled: bool
    toll_vehicle_class: int
    toll_multiplier: float


def _read_float_env(name: str, default: float) -> tuple[float, bool]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default, False
    try:
        return float(raw), True
    except ValueError:
        return default, False


def toll_multiplier_for_class(vehicle_class: int, class2: float = 2.0, class3: float = 3.0) -> float:
    """Google returns the Class 1 (car/SUV) fee; TRB Class 2 = 2.0x, Class 3 = 3.0x. Unknown class -> Class 2."""
    if vehicle_class == 1:
        return 1.0
    if vehicle_class == 3:
        return class3 if class3 > 0 else 3.0
    return class2 if class2 > 0 else 2.0


def build_route_cost_config(
    *,
    distance_rate_per_km: float,
    diesel_price_per_liter: float,
    fuel_km_per_liter: float,
    driver_cost_per_hour: float,
    helper_cost_per_hour: float,
    refrigeration_liters_per_hour_chilled: float,
    refrigeration_liters_per_hour_frozen: float,
    refrigeration_on_return_leg: bool = False,
    distance_rate_configured: bool = True,
    tolls_enabled: bool = True,
    toll_vehicle_class: int = 2,
    toll_class2_multiplier: float = 2.0,
    toll_class3_multiplier: float = 3.0,
) -> RouteCostConfig:
    km_per_liter = fuel_km_per_liter if fuel_km_per_liter > 0 else 7.0
    return {
        "distance_rate_per_km": distance_rate_per_km,
        "distance_rate_configured": distance_rate_configured,
        "diesel_price_per_liter": diesel_price_per_liter,
        "fuel_km_per_liter": km_per_liter,
        "fuel_cost_per_km": diesel_price_per_liter / km_per_liter,
        "driver_cost_per_hour": driver_cost_per_hour,
        "helper_cost_per_hour": helper_cost_per_hour,
        "refrigeration_liters_per_hour_chilled": refrigeration_liters_per_hour_chilled,
        "refrigeration_liters_per_hour_frozen": refrigeration_liters_per_hour_frozen,
        "refrigeration_on_return_leg": refrigeration_on_return_leg,
        "tolls_enabled": tolls_enabled,
        "toll_vehicle_class": toll_vehicle_class if toll_vehicle_class in (1, 2, 3) else 2,
        "toll_multiplier": toll_multiplier_for_class(toll_vehicle_class, toll_class2_multiplier, toll_class3_multiplier),
    }


def load_route_cost_config() -> RouteCostConfig:
    distance_rate, distance_configured = _read_float_env("ROUTE_DISTANCE_COST_PER_KM", 32.0)
    return build_route_cost_config(
        distance_rate_per_km=distance_rate,
        distance_rate_configured=distance_configured,
        diesel_price_per_liter=_read_float_env("ROUTE_DIESEL_PRICE_PER_LITER", 95.0)[0],
        fuel_km_per_liter=_read_float_env("ROUTE_FUEL_KM_PER_LITER", 7.0)[0],
        driver_cost_per_hour=_read_float_env("ROUTE_DRIVER_COST_PER_HOUR", 120.0)[0],
        helper_cost_per_hour=_read_float_env("ROUTE_HELPER_COST_PER_HOUR", 120.0)[0],
        refrigeration_liters_per_hour_chilled=_read_float_env("ROUTE_REFRIGERATION_LITERS_PER_HOUR_CHILLED", 0.8)[0],
        refrigeration_liters_per_hour_frozen=_read_float_env("ROUTE_REFRIGERATION_LITERS_PER_HOUR_FROZEN", 1.2)[0],
        refrigeration_on_return_leg=os.environ.get("ROUTE_REFRIGERATION_ON_RETURN_LEG", "").strip().lower() in {"1", "true", "yes", "on"},
        tolls_enabled=os.environ.get("ROUTE_TOLLS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
        toll_vehicle_class=int(_read_float_env("ROUTE_TOLL_VEHICLE_CLASS", 2.0)[0]),
        toll_class2_multiplier=_read_float_env("ROUTE_TOLL_CLASS2_MULTIPLIER", 2.0)[0],
        toll_class3_multiplier=_read_float_env("ROUTE_TOLL_CLASS3_MULTIPLIER", 3.0)[0],
    )


def leg_toll(toll_info: dict | None, config: RouteCostConfig) -> dict:
    """Toll for one leg from Google's tollInfo ({"present": bool, "price": Class-1 PHP | None}).
    present + price -> amount = price x class multiplier; present + no price -> unknown fee
    (never costed as 0); not present (or tolls disabled) -> nothing."""
    if not config.get("tolls_enabled", True) or not toll_info or not toll_info.get("present"):
        return {"present": False, "amount": 0.0, "unknown": False}
    price = toll_info.get("price")
    if price is None:
        return {"present": True, "amount": 0.0, "unknown": True}
    return {"present": True, "amount": round(float(price) * config["toll_multiplier"], 2), "unknown": False}


def sum_tolls(tolls: list[dict]) -> dict:
    return {
        "present": any(t["present"] for t in tolls),
        "amount": round(sum(t["amount"] for t in tolls), 2),
        "unknown": any(t["unknown"] for t in tolls),
    }


def refrigeration_cost_per_hour(config: RouteCostConfig, *, frozen: bool = False) -> float:
    liters = config["refrigeration_liters_per_hour_frozen"] if frozen else config["refrigeration_liters_per_hour_chilled"]
    return liters * config["diesel_price_per_liter"]


def time_rate_per_hour(config: RouteCostConfig, *, has_helper: bool = False) -> float:
    return config["driver_cost_per_hour"] + (config["helper_cost_per_hour"] if has_helper else 0.0)


def describe_rates(config: RouteCostConfig) -> dict:
    """Live rate values for the 'Rates:' line under the cost table."""
    return {
        "dieselPricePerLiter": config["diesel_price_per_liter"],
        "fuelKmPerLiter": config["fuel_km_per_liter"],
        "fuelCostPerKm": round(config["fuel_cost_per_km"], 2),
        "distanceCostPerKm": config["distance_rate_per_km"],
        "driverCostPerHour": config["driver_cost_per_hour"],
        "helperCostPerHour": config["helper_cost_per_hour"],
        "refrigerationLitersPerHourChilled": config["refrigeration_liters_per_hour_chilled"],
        "refrigerationLitersPerHourFrozen": config["refrigeration_liters_per_hour_frozen"],
        "refrigerationCostPerHourChilled": round(refrigeration_cost_per_hour(config, frozen=False), 2),
        "refrigerationCostPerHourFrozen": round(refrigeration_cost_per_hour(config, frozen=True), 2),
        "refrigerationOnReturnLeg": config["refrigeration_on_return_leg"],
        "tollsEnabled": config["tolls_enabled"],
        "tollVehicleClass": config["toll_vehicle_class"],
        "tollMultiplier": config["toll_multiplier"],
    }


def compute_route_cost_breakdown(
    distance_km: float,
    duration_min: float,
    *,
    config: RouteCostConfig | None = None,
    has_helper: bool = False,
    refrigeration_hours: float = 0.0,
    frozen: bool = False,
    refrigerated: bool = True,
    distance_rate_per_km: float | None = None,
    time_rate_per_hour_override: float | None = None,
    tolls: float = 0.0,
) -> dict:
    """Cost of one leg (`tolls` = known toll fee for the leg, already class-adjusted).
    `refrigeration_hours` is the time the reefer unit runs on this leg
    (0 for a return leg unless ROUTE_REFRIGERATION_ON_RETURN_LEG is on); `refrigerated=False`
    (non-reefer truck) forces refrigeration to 0."""
    rates = config or load_route_cost_config()
    distance_rate = float(distance_rate_per_km if distance_rate_per_km is not None else rates["distance_rate_per_km"])
    time_rate = float(time_rate_per_hour_override if time_rate_per_hour_override is not None else time_rate_per_hour(rates, has_helper=has_helper))

    distance_cost = round(float(distance_km) * distance_rate, 2)
    time_cost = round((float(duration_min) / 60.0) * time_rate, 2)
    fuel_cost = round(float(distance_km) / rates["fuel_km_per_liter"] * rates["diesel_price_per_liter"], 2)
    refrigeration_cost = round(float(refrigeration_hours) * refrigeration_cost_per_hour(rates, frozen=frozen), 2) if refrigerated else 0.0
    toll_cost = round(float(tolls), 2)
    return {
        "distance": distance_cost,
        "time": time_cost,
        "fuel": fuel_cost,
        "refrigeration": refrigeration_cost,
        "tolls": toll_cost,
        "total": round(distance_cost + time_cost + fuel_cost + refrigeration_cost + toll_cost, 2),
    }


def arc_cost_rates(
    config: RouteCostConfig,
    *,
    distance_rate_per_km: float | None = None,
    time_rate_per_hour_override: float | None = None,
    has_helper: bool = False,
    refrigerated: bool = False,
    frozen: bool = False,
) -> tuple[float, float]:
    """(peso per km, peso per hour) used by solvers - the same rates compute_route_cost_breakdown
    applies, folded into per-km (distance + fuel) and per-hour (driver/helper + reefer fuel)."""
    per_km = float(distance_rate_per_km if distance_rate_per_km is not None else config["distance_rate_per_km"]) + config["fuel_cost_per_km"]
    per_hour = float(time_rate_per_hour_override if time_rate_per_hour_override is not None else time_rate_per_hour(config, has_helper=has_helper))
    if refrigerated:
        per_hour += refrigeration_cost_per_hour(config, frozen=frozen)
    return per_km, per_hour


def build_objective_cost_matrix(
    distance_km: list[list[float]],
    duration_min: list[list[float]],
    *,
    objective: str,
    config: RouteCostConfig | None = None,
    distance_rate_per_km: float | None = None,
    time_rate_per_hour_override: float | None = None,
    has_helper: bool = False,
    refrigerated: bool = False,
    frozen: bool = False,
    balanced_time_weight: float = 0.4,
    balanced_distance_weight: float = 0.2,
    balanced_cost_weight: float = 0.4,
) -> list[list[float]]:
    rates = config or load_route_cost_config()
    per_km, per_hour = arc_cost_rates(
        rates,
        distance_rate_per_km=distance_rate_per_km,
        time_rate_per_hour_override=time_rate_per_hour_override,
        has_helper=has_helper,
        refrigerated=refrigerated,
        frozen=frozen,
    )
    n = len(distance_km)
    operating_cost = [
        [distance_km[i][j] * per_km + (duration_min[i][j] / 60.0) * per_hour for j in range(n)]
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
