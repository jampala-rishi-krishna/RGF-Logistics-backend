from __future__ import annotations

import asyncio
import base64
import logging
import os

import httpx

from services import cartrack_limiter

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
            cartrack_limiter.shared.note()  # counts against the shared budget; never delays the live poller
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
            cartrack_limiter.shared.note()  # counts against the shared budget; never delays the live poller
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


class CartrackError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


async def get_json(path: str, params: dict | None = None, *, counter=None, limiter=None, sleep=asyncio.sleep, client_factory=None) -> dict | list:
    """Rate-limited GET for Fleet Health jobs: waits for the shared 20/min budget, retries 429/5xx/timeouts
    after 1, 2, 4, 8 s (honouring Cartrack's Retry-After hint when it is larger), then raises CartrackError."""
    limiter = limiter or cartrack_limiter.shared
    make_client = client_factory or (lambda: httpx.AsyncClient(timeout=30.0))
    delays = cartrack_limiter.RETRY_DELAYS_SECONDS
    last = "no attempt"
    for attempt in range(len(delays) + 1):
        await limiter.acquire()
        if counter is not None:
            counter.add(path)
        status = None
        hint = 0.0
        try:
            async with make_client() as client:
                res = await client.get(f"{_base_url()}{path}", headers=_headers(), params=params)
            status = res.status_code
            if status == 200:
                try:
                    return res.json()
                except ValueError:  # truncated/garbled body (seen once on a 300 KB trips page): retry like a 5xx
                    last = "invalid JSON body"
                    raise httpx.TransportError("invalid JSON body")
            try:
                hint = float(res.headers.get("X-RateLimit-Retry-After-Seconds") or res.headers.get("Retry-After") or 0)
            except ValueError:
                hint = 0.0
            last = f"HTTP {status}"
            if status != 429 and status < 500:
                raise CartrackError(f"Cartrack GET {path} failed: HTTP {status}: {res.text[:200]}", status)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = type(exc).__name__
        if attempt < len(delays):
            await sleep(min(max(delays[attempt], hint), 60.0))
    raise CartrackError(f"Cartrack GET {path} failed after retries ({last})", status)
