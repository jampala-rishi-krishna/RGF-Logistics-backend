from __future__ import annotations

import os
import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)
GOOGLE = "https://maps.googleapis.com"
ROUTES = "https://routes.googleapis.com/directions/v2:computeRoutes"
MATRIX = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
GEOCODE = f"{GOOGLE}/maps/api/geocode/json"
PLACES = f"{GOOGLE}/v1/places:autocomplete"


class OrsError(Exception):
    """Compatibility exception for existing route/optimization handlers."""


def _key() -> str:
    value = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not value:
        raise OrsError("GOOGLE_MAPS_API_KEY is not configured.")
    return value


def _waypoint(lng: float, lat: float) -> dict:
    return {"location": {"latLng": {"latitude": float(lat), "longitude": float(lng)}}}


def _seconds(value: str | None) -> float:
    return float(value[:-1]) if value and value.endswith("s") else 0.0


def _failure(response: httpx.Response, operation: str) -> OrsError:
    try:
        message = response.json().get("error", {}).get("message")
    except ValueError:
        message = None
    logger.error("provider=google endpoint=%s status=%s error=%s", response.request.url.path, response.status_code, message or "unknown")
    return OrsError(message or f"Google {operation} failed (HTTP {response.status_code}).")


async def geocode_address(address: str | None) -> dict | None:
    if not address:
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(GEOCODE, params={"address": address, "components": "country:PH", "key": _key()})
    if response.status_code != 200:
        raise _failure(response, "Geocoding API")
    payload = response.json()
    if payload.get("status") == "ZERO_RESULTS":
        return None
    if payload.get("status") != "OK":
        raise OrsError(f"Google Geocoding API returned {payload.get('status', 'UNKNOWN_ERROR')}.")
    result = payload["results"][0]
    location = result["geometry"]["location"]
    return {"lat": float(location["lat"]), "lng": float(location["lng"]), "place_id": result.get("place_id")}


async def search_addresses(address: str, limit: int = 8) -> list[dict]:
    key = _key()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(PLACES, json={"input": address, "includedRegionCodes": ["ph"], "languageCode": "en"}, headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "suggestions.placePrediction"})
    if response.status_code != 200:
        raise _failure(response, "Places API")
    results = []
    async with httpx.AsyncClient(timeout=15) as client:
        for suggestion in (response.json().get("suggestions") or [])[:min(limit, 10)]:
            prediction = suggestion.get("placePrediction") or {}
            place_id = prediction.get("placeId")
            if not place_id:
                continue
            details = await client.get(f"{GOOGLE}/v1/places/{place_id}", params={"key": key}, headers={"X-Goog-FieldMask": "location"})
            location = details.json().get("location") if details.status_code == 200 else None
            if location and "latitude" in location:
                results.append({"id": place_id, "place_id": place_id, "label": prediction.get("text", {}).get("text", address), "lat": location["latitude"], "lng": location["longitude"], "type": "place", "provider": "google"})
    return results


def _decode_polyline(encoded: str) -> list[list[float]]:
    points, index, lat, lng = [], 0, 0, 0
    while index < len(encoded):
        values = []
        for _ in range(2):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63; index += 1
                result |= (byte & 31) << shift; shift += 5
                if byte < 32: break
            values.append(~(result >> 1) if result & 1 else result >> 1)
        lat += values[0]; lng += values[1]
        points.append([lng / 1e5, lat / 1e5])
    return points


ROUTE_FIELD_MASK = "routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline,routes.legs.distanceMeters,routes.legs.duration,routes.optimizedIntermediateWaypointIndex"
TOLL_FIELD_MASK = ",routes.travelAdvisory.tollInfo,routes.legs.travelAdvisory.tollInfo"


def parse_toll_info(advisory: dict | None) -> dict:
    """Google's travelAdvisory -> {"present", "price"}. Prices are Class 1 (non-commercial) PHP.
    tollInfo present with no estimatedPrice = a toll exists but Google has no fee ("price": None).
    Money in a currency other than PHP is not guessed at, so it also counts as an unknown fee."""
    advisory = advisory or {}
    if "tollInfo" not in advisory:
        return {"present": False, "price": None}
    prices = (advisory.get("tollInfo") or {}).get("estimatedPrice") or []
    php = [m for m in prices if str(m.get("currencyCode", "")).upper() == "PHP"]
    if not php:
        return {"present": True, "price": None}
    return {"present": True, "price": sum(float(m.get("units") or 0) + float(m.get("nanos") or 0) / 1e9 for m in php)}


def _legs_with_tolls(route: dict, include_tolls: bool) -> list[dict]:
    legs = []
    for leg in route.get("legs") or []:
        item = {"distance_km": leg.get("distanceMeters", 0) / 1000, "duration_min": _seconds(leg.get("duration")) / 60}
        if include_tolls:
            item["toll"] = parse_toll_info(leg.get("travelAdvisory"))
        legs.append(item)
    # A single-leg route may carry its toll only at route level.
    if include_tolls and len(legs) == 1 and not legs[0]["toll"]["present"]:
        legs[0]["toll"] = parse_toll_info(route.get("travelAdvisory"))
    return legs


async def fetch_route_polyline(locations: list[list[float]], *, optimize_waypoint_order: bool = False, include_tolls: bool = False, avoid_tolls: bool = False) -> dict:
    """include_tolls requests extraComputations=TOLLS (a higher-billed SKU: only the Calculate flow
    asks for it). avoid_tolls sets routeModifiers.avoidTolls."""
    key = _key()
    body = {"origin": _waypoint(*locations[0]), "destination": _waypoint(*locations[-1]), "intermediates": [_waypoint(*p) for p in locations[1:-1]], "travelMode": "DRIVE", "routingPreference": "TRAFFIC_AWARE", "optimizeWaypointOrder": bool(optimize_waypoint_order)}
    if include_tolls:
        body["extraComputations"] = ["TOLLS"]
    if avoid_tolls:
        body["routeModifiers"] = {"avoidTolls": True}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(ROUTES, json=body, headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": ROUTE_FIELD_MASK + (TOLL_FIELD_MASK if include_tolls else "")})
    logger.info("provider=google endpoint=%s status=%s", "/directions/v2:computeRoutes", response.status_code)
    if response.status_code != 200: raise _failure(response, "Routes API")
    route = (response.json().get("routes") or [None])[0]
    if not route: raise OrsError("Google Routes API returned no route.")
    return {
        "polyline": __import__("json").dumps({"type": "LineString", "coordinates": _decode_polyline(route["polyline"]["encodedPolyline"])}),
        "skippedReason": None,
        "provider": "google",
        "profile": "DRIVE",
        "traffic_aware": True,
        "distance_km": route.get("distanceMeters", 0) / 1000,
        "duration_min": _seconds(route.get("duration")) / 60,
        "legs": _legs_with_tolls(route, include_tolls),
        "tolls_requested": bool(include_tolls),
        "avoid_tolls": bool(avoid_tolls),
        "optimized_waypoint_order": route.get("optimizedIntermediateWaypointIndex") or [],
        "calculated_at": datetime.now(timezone.utc).isoformat(),
        "departure_time": None,
        "fallback_used": False,
        "fallback_reason": None,
    }


async def fetch_route_matrix(locations: list[list[float]]) -> dict:
    if len(locations) < 2: raise OrsError("At least 2 locations are required to build a matrix.")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(MATRIX, json={"origins": [{"waypoint": _waypoint(*p)} for p in locations], "destinations": [{"waypoint": _waypoint(*p)} for p in locations], "travelMode": "DRIVE", "routingPreference": "TRAFFIC_AWARE"}, headers={"X-Goog-Api-Key": _key(), "X-Goog-FieldMask": "originIndex,destinationIndex,distanceMeters,duration,condition"})
    logger.info("provider=google endpoint=%s status=%s", "/distanceMatrix/v2:computeRouteMatrix", response.status_code)
    if response.status_code != 200: raise _failure(response, "Compute Route Matrix")
    n = len(locations); distances = [[0.0] * n for _ in range(n)]; durations = [[0.0] * n for _ in range(n)]
    for item in response.json() if isinstance(response.json(), list) else []:
        i, j = item.get("originIndex"), item.get("destinationIndex")
        if i is not None and j is not None and item.get("condition") in (None, "ROUTE_EXISTS"):
            distances[i][j] = item.get("distanceMeters", 0) / 1000; durations[i][j] = _seconds(item.get("duration")) / 60
    return {"distance_matrix_km": distances, "duration_matrix_min": durations, "provider": "google", "profile": "DRIVE", "traffic_aware": True, "calculated_at": datetime.now(timezone.utc).isoformat(), "departure_time": None, "fallback_used": False, "fallback_reason": None}


async def route_requires_ferry(locations: list[list[float]]) -> str | None:
    return None
