from __future__ import annotations

import os
import json
from datetime import datetime, timezone

import httpx

ORS_BASE_URL = "https://api.openrouteservice.org"
OSRM_BASE_URL = "https://router.project-osrm.org"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
MAPBOX_BASE_URL = "https://api.mapbox.com"
MAPBOX_GEOCODING_URL = f"{MAPBOX_BASE_URL}/search/geocode/v6/forward"
MAPBOX_SEARCHBOX_URL = f"{MAPBOX_BASE_URL}/search/searchbox/v1/forward"
PH_COUNTRY = "ph"
MAPBOX_SEARCH_TYPES = "country,region,postcode,district,place,locality,neighborhood,street,address,poi,category"
MAPBOX_GEOCODE_TYPES = "country,region,postcode,district,place,locality,neighborhood,street,address"


def _mapbox_token() -> str | None:
    if os.environ.get("MAPBOX_ROUTING_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return None
    # MAPBOX_SERVER_TOKEN is canonical. A legacy value is accepted only when
    # it is secret-format; this prevents a URL-restricted browser pk.* token
    # from accidentally becoming a server credential.
    server_token = os.environ.get("MAPBOX_SERVER_TOKEN")
    if server_token:
        return server_token
    legacy_token = os.environ.get("MAPBOX_ACCESS_TOKEN")
    return legacy_token if legacy_token and not legacy_token.startswith("pk.") else None


def _mapbox_profile() -> str:
    profile = os.environ.get("MAPBOX_PROFILE", "driving-traffic").strip()
    return profile if profile in {"driving-traffic", "driving"} else "driving-traffic"


def _normalize_mapbox_feature(feature: dict, *, provider: str, default_label: str) -> dict | None:
    properties = feature.get("properties") or {}
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates") or feature.get("center")
    if not coords and "longitude" in properties and "latitude" in properties:
        coords = [properties["longitude"], properties["latitude"]]
    if not coords or len(coords) < 2:
        return None
    label = (
        properties.get("full_address")
        or properties.get("name")
        or properties.get("place_formatted")
        or properties.get("place_name")
        or properties.get("text")
        or feature.get("place_name")
        or feature.get("text")
        or default_label
    )
    feature_type = properties.get("feature_type") or (feature.get("place_type") or ["location"])[0]
    return {
        "id": feature.get("id") or properties.get("mapbox_id"),
        "label": label,
        "lat": float(coords[1]),
        "lng": float(coords[0]),
        "type": feature_type,
        "provider": provider,
    }


def _validate_matrix(data: dict, provider: str) -> dict:
    distances = data.get("distances")
    durations = data.get("durations")
    if not isinstance(distances, list) or not isinstance(durations, list):
        raise OrsError(f"{provider} matrix response is missing distances or durations.")
    if len(distances) != len(durations) or any(not isinstance(row, list) for row in distances + durations):
        raise OrsError(f"{provider} matrix response is not square.")
    if any(value is None for row in distances + durations for value in row):
        raise OrsError(f"{provider} returned an unreachable road-network edge. Choose road-accessible locations.")
    calculated_at = datetime.now(timezone.utc).isoformat()
    return {
        "distance_matrix_km": [[float(v) / 1000 for v in row] for row in distances],
        "duration_matrix_min": [[float(v) / 60 for v in row] for row in durations],
        "provider": provider,
        "profile": _mapbox_profile() if provider == "mapbox" else "driving",
        "traffic_aware": provider == "mapbox" and _mapbox_profile() == "driving-traffic",
        "calculated_at": calculated_at,
        "departure_time": calculated_at if provider == "mapbox" and _mapbox_profile() == "driving-traffic" else None,
        "fallback_used": provider != "mapbox",
        "fallback_reason": None,
    }


async def _mapbox_matrix(locations: list[list[float]], token: str) -> dict:
    """Build a complete directional matrix in bounded Mapbox requests.

    Mapbox driving-traffic has a finite coordinate limit. Five sources plus five
    destinations keeps each request within the traffic profile limit and preserves
    asymmetric distance/duration values while reconstructing the full matrix.
    """
    n = len(locations)
    batch_size = max(2, min(int(os.environ.get("MAPBOX_MATRIX_BATCH_SIZE", "5")), 5))
    distances = [[None for _ in range(n)] for _ in range(n)]
    durations = [[None for _ in range(n)] for _ in range(n)]
    async with httpx.AsyncClient(timeout=20.0) as client:
        for source_start in range(0, n, batch_size):
            source_indices = list(range(source_start, min(source_start + batch_size, n)))
            for destination_start in range(0, n, batch_size):
                destination_indices = list(range(destination_start, min(destination_start + batch_size, n)))
                request_locations = [locations[i] for i in source_indices + destination_indices]
                coords = ";".join(f"{lng},{lat}" for lng, lat in request_locations)
                response = await client.get(
                    f"{MAPBOX_BASE_URL}/directions-matrix/v1/mapbox/{_mapbox_profile()}/{coords}",
                    params={
                        "access_token": token,
                        "sources": ";".join(str(i) for i in range(len(source_indices))),
                        "destinations": ";".join(str(i) for i in range(len(source_indices), len(request_locations))),
                        "annotations": "distance,duration",
                    },
                )
                if response.status_code != 200:
                    raise OrsError(f"Mapbox Matrix API returned HTTP {response.status_code}.")
                payload = response.json()
                block_distances = payload.get("distances") or []
                block_durations = payload.get("durations") or []
                if len(block_distances) != len(source_indices) or len(block_durations) != len(source_indices):
                    raise OrsError("Mapbox Matrix response dimensions do not match the requested batch.")
                for local_i, global_i in enumerate(source_indices):
                    for local_j, global_j in enumerate(destination_indices):
                        distances[global_i][global_j] = block_distances[local_i][local_j]
                        durations[global_i][global_j] = block_durations[local_i][local_j]
    return _validate_matrix({"distances": distances, "durations": durations}, "mapbox")


async def _mapbox_search(address: str, limit: int = 8) -> list[dict] | None:
    token = _mapbox_token()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.get(
                MAPBOX_GEOCODING_URL,
                params={
                    "access_token": token,
                    "q": address,
                    "country": PH_COUNTRY,
                    "types": MAPBOX_GEOCODE_TYPES,
                    "autocomplete": "true",
                    "limit": min(limit, 10),
                    "language": "en",
                },
            )
    except httpx.HTTPError:
        return None
    if res.status_code != 200:
        return None
    results = []
    for feature in res.json().get("features") or []:
        normalized = _normalize_mapbox_feature(feature, provider="mapbox", default_label=address)
        if normalized:
            results.append(normalized)
    return results


async def _mapbox_searchbox(address: str, limit: int = 8) -> list[dict] | None:
    token = _mapbox_token()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.get(
                MAPBOX_SEARCHBOX_URL,
                params={
                    "access_token": token,
                    "q": address,
                    "country": PH_COUNTRY,
                    "types": MAPBOX_SEARCH_TYPES,
                    "limit": min(limit, 10),
                    "language": "en",
                },
            )
    except httpx.HTTPError:
        return None
    if res.status_code != 200:
        return None
    results = []
    for feature in res.json().get("features") or []:
        normalized = _normalize_mapbox_feature(feature, provider="mapbox-searchbox", default_label=address)
        if normalized:
            results.append(normalized)
    return results


async def _fallback_geocode(address: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "IntelliFleet/1.0 route planner"}) as client:
            res = await client.get(NOMINATIM_URL, params={"q": address, "format": "jsonv2", "countrycodes": "ph", "limit": 1})
    except httpx.HTTPError:
        return None
    if res.status_code != 200:
        return None
    places = res.json()
    if not places:
        return None
    return {"lat": float(places[0]["lat"]), "lng": float(places[0]["lon"])}


async def search_addresses(address: str, limit: int = 8) -> list[dict]:
    """Return detailed Philippine place suggestions for an autocomplete field."""
    query = str(address).strip()
    if not query:
        return []
    combined: list[dict] = []
    mapbox_searchbox_results = await _mapbox_searchbox(query, limit)
    if mapbox_searchbox_results is not None:
        combined.extend(mapbox_searchbox_results)
    mapbox_results = await _mapbox_search(query, limit)
    if mapbox_results is not None:
        combined.extend(mapbox_results)
    if combined:
        deduped: list[dict] = []
        seen: set[tuple[str, float, float]] = set()
        for result in combined:
            key = (str(result.get("label") or "").strip().lower(), round(float(result["lat"]), 6), round(float(result["lng"]), 6))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(result)
        return deduped[: min(limit, 10)]
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "IntelliFleet/1.0 route planner"}) as client:
            res = await client.get(
                NOMINATIM_URL,
                params={"q": f"{query}, Philippines", "format": "jsonv2", "addressdetails": 1, "dedupe": 1, "limit": min(limit, 10)},
            )
    except httpx.HTTPError:
        return []
    if res.status_code != 200:
        return []
    suggestions = []
    for place in res.json() or []:
        suggestions.append({
            "id": place.get("osm_id"),
            "label": place.get("display_name") or query,
            "lat": float(place["lat"]),
            "lng": float(place["lon"]),
            "type": place.get("type") or "location",
            "provider": "nominatim",
        })
    return suggestions


async def _fallback_matrix(locations: list[list[float]]) -> dict:
    coords = ";".join(f"{lng},{lat}" for lng, lat in locations)
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.get(f"{OSRM_BASE_URL}/table/v1/driving/{coords}", params={"annotations": "distance,duration"})
    if res.status_code != 200:
        raise OrsError(f"Road routing fallback returned {res.status_code}: {res.text[:200]}")
    data = res.json()
    return _validate_matrix(data, "osrm")


async def _fallback_polyline(locations: list[list[float]]) -> dict:
    coords = ";".join(f"{lng},{lat}" for lng, lat in locations)
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.get(
            f"{OSRM_BASE_URL}/route/v1/driving/{coords}",
            params={"overview": "full", "geometries": "geojson"},
        )
    if res.status_code != 200:
            return {"polyline": None, "skippedReason": f"Road routing fallback returned {res.status_code}.", "provider": "osrm", "profile": "driving", "traffic_aware": False}
    routes = res.json().get("routes") or []
    geometry = routes[0].get("geometry") if routes else None
    route = routes[0] if routes else {}
    calculated_at = datetime.now(timezone.utc).isoformat()
    return {"polyline": json.dumps(geometry) if geometry else None, "skippedReason": None if geometry else "No road geometry returned.", "provider": "osrm", "profile": "driving", "traffic_aware": False, "calculated_at": calculated_at, "departure_time": None,
            "distance_km": float(route.get("distance", 0)) / 1000, "duration_min": float(route.get("duration", 0)) / 60}


class OrsError(Exception):
    pass


def _api_key() -> str:
    key = os.environ.get("ORS_API_KEY")
    if not key:
        raise OrsError("ORS_API_KEY is not configured.")
    return key


async def fetch_route_matrix(locations: list[list[float]]) -> dict:
    """POST /v2/matrix/driving-car. locations are [lng, lat] pairs. Ported verbatim from
    routes-module/optimization-module's lib/ors-client.js fetchRouteMatrix: dedupes identical
    coordinates before calling ORS (fewer credits), then expands the compact response back to
    the full N x N matrix aligned with the original (possibly-duplicated) node order. Distance
    is already km (requested); duration is converted seconds -> minutes."""
    if not locations or len(locations) < 2:
        raise OrsError("At least 2 locations are required to build a matrix.")
    mapbox_token = _mapbox_token()
    if mapbox_token:
        try:
            return await _mapbox_matrix(locations, mapbox_token)
        except (httpx.HTTPError, OrsError):
            pass
    try:
        api_key = _api_key()
    except OrsError:
        return await _fallback_matrix(locations)

    unique_locations: list[list[float]] = []
    unique_key_to_idx: dict[str, int] = {}
    node_to_unique_idx: list[int] = []
    for lng, lat in locations:
        key = f"{lng},{lat}"
        if key not in unique_key_to_idx:
            unique_key_to_idx[key] = len(unique_locations)
            unique_locations.append([lng, lat])
        node_to_unique_idx.append(unique_key_to_idx[key])

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.post(
                f"{ORS_BASE_URL}/v2/matrix/driving-car",
                headers={"Authorization": api_key, "Content-Type": "application/json"},
                json={"locations": unique_locations, "metrics": ["distance", "duration"], "units": "km"},
            )
    except httpx.TimeoutException:
        raise OrsError("ORS Matrix API timed out after 20s. Road travel data unavailable.")
    except httpx.HTTPError as e:
        raise OrsError(f"ORS Matrix API request failed: {e}")

    if res.status_code != 200:
        return await _fallback_matrix(locations)

    data = res.json()
    compact_distances = data.get("distances")
    compact_durations = data.get("durations")
    if compact_distances is None or compact_durations is None:
        raise OrsError("ORS Matrix response is missing distances or durations fields.")
    if any(value is None for row in compact_distances + compact_durations for value in row):
        raise OrsError("OpenRouteService returned an unreachable road-network edge. Choose road-accessible locations.")

    n = len(locations)
    distance_matrix_km = [
        [compact_distances[node_to_unique_idx[i]][node_to_unique_idx[j]] for j in range(n)] for i in range(n)
    ]
    duration_matrix_min = [
        [compact_durations[node_to_unique_idx[i]][node_to_unique_idx[j]] / 60.0 for j in range(n)] for i in range(n)
    ]
    calculated_at = datetime.now(timezone.utc).isoformat()
    return {"distance_matrix_km": distance_matrix_km, "duration_matrix_min": duration_matrix_min,
            "provider": "ors", "profile": "driving-car", "traffic_aware": False,
            "calculated_at": calculated_at, "departure_time": None}


async def route_requires_ferry(ordered_lnglat_coords: list[list[float]]) -> str | None:
    """Return a human-readable issue if the requested route cannot be kept on-road.

    Mapbox explicitly supports `exclude=ferry` and reports violations when that exclusion
    cannot be honored. For optimization and planning, we use this as a strict guard so that
    island-to-island paths do not silently render as acceptable road routes.
    """
    if len(ordered_lnglat_coords) < 2:
        return "At least 2 locations are required to validate a route."

    mapbox_token = _mapbox_token()
    coords = ";".join(f"{lng},{lat}" for lng, lat in ordered_lnglat_coords)
    if mapbox_token:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.get(
                f"{MAPBOX_BASE_URL}/directions/v5/mapbox/{_mapbox_profile()}/{coords}",
                params={
                    "access_token": mapbox_token,
                    "overview": "false",
                    "steps": "true",
                    "geometries": "geojson",
                    "exclude": "ferry",
                },
            )
        if res.status_code != 200:
            return f"Mapbox road-only routing returned HTTP {res.status_code}."
        payload = res.json()
        routes = payload.get("routes") or []
        if not routes:
            return "No road-only route was found between these locations. Ferry travel would be required."
        notifications = payload.get("notifications") or []
        for notification in notifications:
            if str(notification.get("type")).lower() == "violation":
                details = notification.get("details") or {}
                message = str(details.get("message") or notification.get("message") or "").lower()
                if "ferry" in message or notification.get("subtype") == "ferry":
                    return "The route requires ferry travel, so a road-only path is not available."
        for route in routes:
            for leg in route.get("legs") or []:
                for step in leg.get("steps") or []:
                    if str(step.get("mode") or "").lower() == "ferry":
                        return "The route requires ferry travel, so a road-only path is not available."
        return None

    try:
        api_key = _api_key()
    except OrsError:
        return None
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(
            f"{ORS_BASE_URL}/v2/directions/driving-car/geojson",
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"coordinates": ordered_lnglat_coords, "options": {"avoid_features": ["ferries"]}},
        )
    if res.status_code != 200:
        return "The route could not be calculated without ferries."
    geojson = res.json()
    features = geojson.get("features") or []
    if not features:
        return "No road-only route was found between these locations. Ferry travel would be required."
    return None


async def fetch_route_polyline(ordered_lnglat_coords: list[list[float]]) -> dict:
    """POST /v2/directions/driving-car/geojson. Never raises - every failure path returns
    {polyline: None, skippedReason}, matching the original's best-effort/never-fatal design."""
    api_key = os.environ.get("ORS_API_KEY")
    mapbox_token = _mapbox_token()
    if mapbox_token and len(ordered_lnglat_coords) >= 2:
        coords = ";".join(f"{lng},{lat}" for lng, lat in ordered_lnglat_coords)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.get(
                    f"{MAPBOX_BASE_URL}/directions/v5/mapbox/{_mapbox_profile()}/{coords}",
                    params={"access_token": mapbox_token, "overview": "full", "geometries": "geojson", "steps": "false", "exclude": "ferry"},
                )
            if res.status_code == 200:
                routes = res.json().get("routes") or []
                geometry = routes[0].get("geometry") if routes else None
                if geometry:
                    calculated_at = datetime.now(timezone.utc).isoformat()
                    return {"polyline": json.dumps(geometry), "skippedReason": None, "provider": "mapbox", "profile": _mapbox_profile(), "traffic_aware": _mapbox_profile() == "driving-traffic", "calculated_at": calculated_at, "departure_time": calculated_at if _mapbox_profile() == "driving-traffic" else None,
                            "distance_km": float(routes[0].get("distance", 0)) / 1000, "duration_min": float(routes[0].get("duration", 0)) / 60}
        except httpx.HTTPError:
            pass
    if not api_key:
        result = await _fallback_polyline(ordered_lnglat_coords)
        result["fallback_used"] = True
        result["fallback_reason"] = "Mapbox server token is not configured."
        return result
    if len(ordered_lnglat_coords) < 2:
        return {"polyline": None, "skippedReason": "Fewer than 2 points - nothing to draw a line between."}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(
                f"{ORS_BASE_URL}/v2/directions/driving-car/geojson",
                headers={"Authorization": api_key, "Content-Type": "application/json"},
                json={"coordinates": ordered_lnglat_coords, "options": {"avoid_features": ["ferries"]}},
            )
        if res.status_code != 200:
            result = await _fallback_polyline(ordered_lnglat_coords)
            result["fallback_used"] = True
            result["fallback_reason"] = f"ORS directions returned HTTP {res.status_code}."
            return result
        geojson = res.json()
        features = geojson.get("features") or []
        geometry = features[0].get("geometry") if features else None
        if geometry:
            import json as _json

            summary = (features[0].get("properties") or {}).get("summary") or {}
            calculated_at = datetime.now(timezone.utc).isoformat()
            return {"polyline": _json.dumps(geometry), "skippedReason": None, "provider": "ors", "profile": "driving-car", "traffic_aware": False, "calculated_at": calculated_at, "departure_time": None,
                    "distance_km": float(summary.get("distance", 0)) / 1000, "duration_min": float(summary.get("duration", 0)) / 60}
        return {"polyline": None, "skippedReason": "ORS response had no geometry.", "provider": "ors", "profile": "driving-car", "traffic_aware": False, "calculated_at": datetime.now(timezone.utc).isoformat(), "departure_time": None, "distance_km": 0, "duration_min": 0}
    except Exception as e:
        result = await _fallback_polyline(ordered_lnglat_coords)
        result["fallback_used"] = True
        result["fallback_reason"] = f"ORS directions failed: {type(e).__name__}."
        return result


async def geocode_address(address: str | None) -> dict | None:
    """GET /geocode/search (Pelias). Returns {lat, lng} or None if no confident match. Raises
    OrsError only when ORS_API_KEY itself is missing (matches optimization-data.js exactly)."""
    if not address or not str(address).strip():
        return None
    mapbox_searchbox_results = await _mapbox_searchbox(str(address).strip(), 1)
    if mapbox_searchbox_results:
        return mapbox_searchbox_results[0]
    mapbox_results = await _mapbox_search(str(address).strip(), 1)
    if mapbox_results:
        return mapbox_results[0]
    try:
        api_key = _api_key()
    except OrsError:
        return await _fallback_geocode(str(address).strip())

    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(
            f"{ORS_BASE_URL}/geocode/search",
            params={"api_key": api_key, "text": str(address).strip(), "size": 1},
        )
    if res.status_code != 200:
        return await _fallback_geocode(str(address).strip())
    geojson = res.json()
    features = geojson.get("features") or []
    if not features:
        return await _fallback_geocode(str(address).strip())
    geometry = features[0].get("geometry") or {}
    coords = geometry.get("coordinates")
    if not coords or len(coords) < 2:
        return None
    lng, lat = coords[0], coords[1]
    return {"lat": lat, "lng": lng}
