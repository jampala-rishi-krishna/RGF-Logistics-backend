from __future__ import annotations

import asyncio
import base64
import logging
import os

import httpx

logger = logging.getLogger("cartrack_client")

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0


def _base_url() -> str:
    return os.environ.get("CARTRACK_BASE_URL", "https://fleetapi-ph.cartrack.com")


def _headers() -> dict:
    username = os.environ["CARTRACK_USERNAME"]
    api_key = os.environ["CARTRACK_API_KEY"]
    encoded = base64.b64encode(f"{username}:{api_key}".encode()).decode()
    return {"Authorization": f"Basic {encoded}", "Accept": "application/json", "Content-Type": "application/json"}


async def get_all_vehicles() -> list | None:
    """GET /rest/vehicles - full vehicle roster. Used once at startup to report unmatched
    plates. Rate limit: 1000 req/min."""
    url = f"{_base_url()}/rest/vehicles"
    for attempt in range(MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.get(url, headers=_headers())
            if res.status_code == 429:
                retry_after = int(res.headers.get("X-RateLimit-Retry-After-Seconds", "60"))
                logger.warning(f"[Cartrack] Rate limited on /vehicles, retrying after {retry_after}s")
                await asyncio.sleep(retry_after)
                continue
            if res.status_code == 403:
                logger.error("[Cartrack] 403 on /vehicles - check API key scopes (Vehicle List scope required)")
                return None
            if res.status_code != 200:
                logger.error(f"[Cartrack] /vehicles returned {res.status_code}: {res.text[:300]}")
                return None
            data = res.json()
            return (data.get("data") if isinstance(data, dict) else data) or []
        except httpx.TimeoutException:
            logger.warning(f"[Cartrack] Timeout on /vehicles, attempt {attempt + 1}/{MAX_RETRIES}")
            await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
    logger.error("[Cartrack] /vehicles failed after all retries")
    return None


async def get_vehicle_statuses() -> list | None:
    """GET /rest/vehicles/status - live GPS/telemetry for every vehicle in one call. Rate
    limit: 60 req/min - comfortably inside that at a 30s poll cadence."""
    url = f"{_base_url()}/rest/vehicles/status"
    for attempt in range(MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.get(url, headers=_headers())
            if res.status_code == 429:
                retry_after = int(res.headers.get("X-RateLimit-Retry-After-Seconds", "60"))
                logger.warning(f"[Cartrack] Rate limited on /vehicles/status, retrying after {retry_after}s")
                await asyncio.sleep(retry_after)
                continue
            if res.status_code == 403:
                logger.error("[Cartrack] 403 on /vehicles/status - check API key scopes (Vehicle Status scope required)")
                return None
            if res.status_code != 200:
                logger.error(f"[Cartrack] /vehicles/status returned {res.status_code}: {res.text[:300]}")
                return None
            data = res.json()
            return (data.get("data") if isinstance(data, dict) else data) or []
        except httpx.TimeoutException:
            logger.warning(f"[Cartrack] Timeout on /vehicles/status, attempt {attempt + 1}/{MAX_RETRIES}")
            await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
    logger.error("[Cartrack] /vehicles/status failed after all retries")
    return None


def parse_vehicle_status(raw: dict) -> dict:
    """Parses a real Cartrack PH /rest/vehicles/status entry - confirmed field names (ported
    using the provider response fields: heading is "bearing", odometer is
    top-level meters, fuel is nested under "precentage_left" (typo in the real API, not ours)."""
    location = raw.get("location") or {}
    fuel = raw.get("fuel") or {}
    odometer_meters = raw.get("odometer") or 0

    return {
        "cartrack_id": str(raw.get("vehicle_id") or ""),
        "lat": float(location.get("latitude") or 0.0),
        "lng": float(location.get("longitude") or 0.0),
        "speed_kmh": float(raw.get("speed") or 0),
        "heading": int(raw.get("bearing") or 0),
        "ignition_on": bool(raw.get("ignition") or False),
        "odometer_km": round(float(odometer_meters) / 1000, 2),
        "event_time": raw.get("event_ts"),
        "registration": raw.get("registration") or "",
        "fuel_pct": fuel.get("precentage_left") or 0,
        "address": location.get("position_description") or "",
    }
