"""Google Route Optimization adapter for the Rarechain fleet workflow."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
import google.auth
from google.auth.transport.requests import Request

from services.optimization_data import FleetData
from services.route_costs import arc_cost_rates, load_route_cost_config

logger = logging.getLogger(__name__)
ENDPOINT = "https://routeoptimization.googleapis.com/v1/projects/rarechain-logistics-508805:optimizeTours"
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class GoogleOptimizationError(Exception):
    pass


def _duration(minutes: float) -> str:
    return f"{max(0, minutes) * 60:g}s"


def _timestamp(minutes: float) -> str:
    base = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (base + timedelta(minutes=max(0, minutes))).isoformat().replace("+00:00", "Z")


def build_request(data: FleetData) -> dict:
    vehicles = []
    cost_config = load_route_cost_config()
    for v in data.vehicles:
        # Same per-km / per-hour rates the single-route cost table uses (distance + fuel per km;
        # driver [+ reefer fuel] per hour), so the solver optimises the real operating cost.
        cost_per_km, cost_per_hour = arc_cost_rates(
            cost_config,
            distance_rate_per_km=v.cost_per_km,
            time_rate_per_hour_override=v.cost_per_hour,
            refrigerated=any(str(c).strip().lower() not in ("", "ambient") for c in (getattr(v, "temperature_capabilities", None) or [])),
        )
        vehicle = {
            "label": str(v.id),
            "startLocation": {"latitude": v.start_lat, "longitude": v.start_lng},
            "loadLimits": {"weightKg": {"maxLoad": v.capacity_kg}},
            "costPerKilometer": cost_per_km,
            "costPerHour": cost_per_hour,
        }
        if getattr(v, "has_end_location", True):
            vehicle["endLocation"] = {"latitude": v.end_lat, "longitude": v.end_lng}
        if v.shift_start or v.shift_end:
            vehicle["startTimeWindows"] = [{"startTime": _timestamp(v.shift_start), "endTime": _timestamp(v.shift_end)}]
        vehicles.append(vehicle)
    shipments = []
    for s in data.shipments:
        visit = {
            "arrivalLocation": {"latitude": s.lat, "longitude": s.lng},
            "duration": _duration(s.service_time_min),
            "timeWindows": [{"startTime": _timestamp(s.time_window_start), "endTime": _timestamp(s.time_window_end)}],
        }
        shipments.append({
            "label": str(s.id),
            "deliveries": [{"arrivalLocation": visit["arrivalLocation"], "duration": visit["duration"], "timeWindows": visit["timeWindows"], "loadDemands": {"weightKg": {"amount": s.demand_kg}}}],
            "penaltyCost": 100000000,
        })
    return {
        "model": {
            "globalStartTime": _timestamp(0),
            "globalEndTime": _timestamp(24 * 60),
            "vehicles": vehicles,
            "shipments": shipments,
        },
        "timeout": "30s",
    }


def _error(response: httpx.Response) -> GoogleOptimizationError:
    try:
        payload = response.json()
        error = payload.get("error", {})
        message = error.get("message", "unknown Google error")
        code = error.get("status", response.status_code)
    except ValueError:
        message, code = "invalid Google error response", response.status_code
    logger.error("provider=google operation=optimizeTours endpoint=%s status=%s code=%s error=%s", "/v1/projects/rarechain-logistics-508805:optimizeTours", response.status_code, code, message)
    return GoogleOptimizationError(f"Google Route Optimization failed ({code}): {message}")


async def optimize(data: FleetData) -> dict:
    try:
        credentials, project_id = google.auth.default(scopes=SCOPES)
        if not credentials.valid:
            credentials.refresh(Request())
        access_token = credentials.token
    except Exception as exc:
        logger.error("provider=google operation=adc_authentication status=configuration_error error=%s", exc)
        raise GoogleOptimizationError("Google Route Optimization requires configured Application Default Credentials (ADC).") from exc
    if not access_token:
        raise GoogleOptimizationError("ADC did not provide an OAuth2 access token.")
    body = build_request(data)
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(ENDPOINT, json=body, headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"})
    logger.info("provider=google operation=optimizeTours endpoint=%s status=%s", "/v1/projects/rarechain-logistics-508805:optimizeTours", response.status_code)
    if response.status_code != 200:
        raise _error(response)
    return response.json()


def parse_response(payload: dict, data: FleetData) -> list[dict]:
    by_index = {i: s for i, s in enumerate(data.shipments)}
    routes = []
    for index, route in enumerate(payload.get("routes", [])):
        vehicle = data.vehicles[index] if index < len(data.vehicles) else None
        if vehicle is None:
            continue
        ids = []
        arrivals = []
        for visit in route.get("visits", []):
            shipment = by_index.get(visit.get("shipmentIndex"))
            if shipment is not None and not visit.get("isPickup", False):
                ids.append(str(shipment.id))
                arrivals.append(None)
        routes.append({"vehicle_id": str(vehicle.id), "stop_sequence": ids, "arrival_min": arrivals, "slack_min": [], "total_distance_km": float(route.get("metrics", {}).get("travelDistanceMeters", 0)) / 1000, "total_duration_min": float(str(route.get("metrics", {}).get("totalDuration", "0s")).rstrip("s")) / 60})
    return routes
