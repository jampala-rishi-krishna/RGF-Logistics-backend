from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from services import fleet_static_cache, gps_cadence, live_gps_store
from services.cartrack_client import get_all_vehicles, get_vehicle_statuses, parse_vehicle_status
from services.ws_manager import manager

logger = logging.getLogger("cartrack_poller")

_poll_cycle_count = 0


def _normalize_plate(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


async def poll_cartrack_and_update() -> None:
    """Live GPS pipeline: one bulk Cartrack call per cycle, in-memory only.

    No database session is opened here, not even to look up vehicles - the
    plate -> vehicle_id map comes from the in-memory fleet_static_cache
    (loaded once at startup, refreshed on vehicle changes). Results are kept
    only in live_gps_store and pushed to the frontend over the existing
    WebSocket - only for vehicles whose fix actually changed; nothing is written to Postgres."""
    global _poll_cycle_count
    _poll_cycle_count += 1
    cycle = _poll_cycle_count

    logger.info(f"[Poller] Starting cycle #{cycle}")

    raw_statuses = await get_vehicle_statuses()
    if raw_statuses is None:
        logger.error("[Poller] Failed to fetch vehicle statuses from Cartrack")
        return
    if not raw_statuses:
        logger.warning("[Poller] Cartrack returned an empty vehicle status list")
        return

    gps_cadence.note_poll()
    parsed = [parse_vehicle_status(r) for r in raw_statuses]
    plate_to_id = fleet_static_cache.plate_to_id()

    matched = 0
    unmatched: list[str] = []
    updates: list[dict] = []

    for status in parsed:
        registration = status["registration"]
        normalized = _normalize_plate(registration)
        vehicle_id = plate_to_id.get(normalized)
        if not vehicle_id:
            unmatched.append(registration or "(missing registration)")
            continue

        if status["lat"] == 0.0 and status["lng"] == 0.0:
            logger.info(f"[Poller] Skipping {registration}: no GPS fix yet (0,0)")
            continue

        entry, changed = live_gps_store.upsert_with_change(
            normalized,
            lat=status["lat"],
            lng=status["lng"],
            heading=status["heading"],
            speed_kph=status["speed_kmh"],
            fuel_pct=status["fuel_pct"],
            ignition_on=status["ignition_on"],
            address=status["address"],
            event_time=status["event_time"],
        )
        matched += 1
        gps_cadence.record(normalized, entry["gps_time"], status["speed_kmh"], changed=changed)
        if not changed:
            continue  # same fix as last poll (a parked truck re-reports one event_ts for hours): nothing to push
        updates.append(
            {
                "type": "VEHICLE_POSITION_UPDATE",
                "vehicle_id": vehicle_id,
                "plate_no": registration,
                "current_lat": entry["lat"],
                "current_lng": entry["lng"],
                "heading": entry["heading"],
                "speed_kph": entry["speed_kph"],
                "ignition_on": entry["ignition_on"],
                "fuel_pct": entry["fuel_pct"],
                "status": entry["status"],
                "address": entry["address"] or "",
                "gps_time": entry["gps_time"],
                "last_updated": entry["last_updated"].isoformat(),
            }
        )

    for payload in updates:
        await manager.broadcast(payload)
    # One tiny message per poll so the browser can tell "feed alive, nothing moved" (a parked fleet sends no
    # position updates at all) from "feed dead".
    await manager.broadcast({"type": "GPS_HEARTBEAT", "polled_at": datetime.now(timezone.utc).isoformat(),
                             "matched": matched, "changed": len(updates)})

    logger.info(f"[Poller] Cycle #{cycle} complete: {matched} matched, {len(updates)} changed/broadcast, {len(unmatched)} unmatched ({unmatched})")


async def report_unmatched_roster_on_startup() -> None:
    """One-shot startup check: report which Cartrack registrations don't correspond to any
    plate_no in our own vehicles table yet."""
    ct_vehicles = await get_all_vehicles()
    if not ct_vehicles:
        logger.warning("[Poller] Could not fetch Cartrack vehicle roster on startup")
        return

    our_plates = set(fleet_static_cache.plate_to_id().keys())

    unmatched = [v.get("registration") for v in ct_vehicles if _normalize_plate(v.get("registration")) not in our_plates]
    logger.info(
        f"[Poller] Startup check: {len(ct_vehicles)} Cartrack vehicles, "
        f"{len(ct_vehicles) - len(unmatched)} already match a plate_no in our vehicles table, "
        f"{len(unmatched)} unmatched: {unmatched}"
    )
