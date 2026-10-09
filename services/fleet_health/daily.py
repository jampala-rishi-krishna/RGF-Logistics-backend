"""Pure calculators for one vehicle-day (no I/O): trips + status samples -> one vehicle_daily_stats row.

Formulas (shown in the UI tooltip next to each number):
  km_driven     = (last trip end_odometer - first trip start_odometer) / 1000   [km]
                  fallback 1: sum(trip_distance) / 1000
                  fallback 2: sum(end_odometer - start_odometer per trip) / 1000
  engine_seconds= sum(trip_duration_seconds)            (engine hours = seconds / 3600)
  idle at stop  = estimate: idle seconds of trips whose start OR end is within 200 m of Mets, Glacier, or an SO
                  delivery point assigned to that truck that day; every other trip's idle is "elsewhere"
  electrical    = 24 V when the running average voltage > 20 V, else 12 V
  refuel        = fuel % rises > 10 points between two samples while stationary -> litres = rise% x capacity
  parked drop   = fuel % falls > 5 points within 2 h with the ignition OFF -> litres = drop% x capacity ("check")
A day belongs to Asia/Manila: 00:00-23:59. A trip belongs to the day it STARTS.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, datetime, timedelta

from services.fleet_health import config

_TZ_SHORT = re.compile(r"([+-]\d{2})$")


def parse_ts(value) -> datetime | None:
    """Cartrack timestamps look like '2026-10-01 04:59:49+08'."""
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    text = _TZ_SHORT.sub(r"\1:00", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=config.MANILA)


def manila_day(ts: datetime) -> date:
    return ts.astimezone(config.MANILA).date()


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _num(value, default=0.0) -> float:
    try:
        return float(value) if value is not None and value != "" else default
    except (TypeError, ValueError):
        return default


def _int(value) -> int:
    return int(_num(value, 0))


def dedupe_trips(trips: list[dict]) -> tuple[list[dict], int]:
    """Drop repeated trip_ids (a truncated-and-retried page can return rows twice). Rows without an id are kept."""
    seen: set = set()
    unique: list[dict] = []
    for trip in trips:
        trip_id = trip.get("trip_id")
        if trip_id is not None:
            if trip_id in seen:
                continue
            seen.add(trip_id)
        unique.append(trip)
    return unique, len(trips) - len(unique)


def engine_time(day_trips: list[dict]) -> dict:
    """Engine seconds = the UNION of the trips' [start, start + duration] intervals, so time that two trips
    claim twice is counted once. Always <= 86,400 and <= (last trip end - first trip start); if the raw sum would
    break either bound it is capped and the cap is reported."""
    intervals = []
    for trip in day_trips:
        started = parse_ts(trip.get("start_timestamp"))
        duration = _int(trip.get("trip_duration_seconds"))
        if started is not None and duration > 0:
            intervals.append((started, started + timedelta(seconds=duration)))
    intervals.sort()
    union = 0.0
    overlap = 0.0
    current_start = current_end = None
    for start, end in intervals:
        if current_end is None or start > current_end:
            if current_end is not None:
                union += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
        else:
            overlap += (min(current_end, end) - start).total_seconds()
            current_end = max(current_end, end)
    if current_end is not None:
        union += (current_end - current_start).total_seconds()
    span = (max(e for _, e in intervals) - intervals[0][0]).total_seconds() if intervals else 0.0
    ceiling = min(86400.0, span) if intervals else 0.0
    seconds = int(round(min(union, ceiling)))
    return {"seconds": seconds, "overlap_seconds": int(round(overlap)), "capped": union > ceiling + 0.5, "raw_sum": sum(_int(t.get("trip_duration_seconds")) for t in day_trips)}


def trips_of_day(trips: list[dict], day: date) -> list[dict]:
    result = []
    for trip in trips:
        started = parse_ts(trip.get("start_timestamp"))
        if started is not None and manila_day(started) == day:
            result.append(trip)
    return sorted(result, key=lambda t: parse_ts(t.get("start_timestamp")))


def _near(coords, sites, radius=config.AT_STOP_RADIUS_M) -> bool:
    if not isinstance(coords, dict) or coords.get("latitude") is None or coords.get("longitude") is None:
        return False
    return any(haversine_m(float(coords["latitude"]), float(coords["longitude"]), lat, lng) <= radius for lat, lng in sites)


def compute_distance(day_trips: list[dict]) -> tuple[float | None, float | None, float | None, str, int]:
    """(odometer_start_km, odometer_end_km, km_driven, source, trips_missing_distance)."""
    missing = sum(1 for t in day_trips if t.get("trip_distance") in (None, ""))
    starts = [_num(t["start_odometer"]) for t in day_trips if t.get("start_odometer") not in (None, "")]
    ends = [_num(t["end_odometer"]) for t in day_trips if t.get("end_odometer") not in (None, "")]
    odo_start = (starts[0] if starts else None)
    odo_end = (ends[-1] if ends else None)
    start_km = round(odo_start / 1000, 2) if odo_start is not None else None
    end_km = round(odo_end / 1000, 2) if odo_end is not None else None
    if odo_start is not None and odo_end is not None and odo_end >= odo_start:
        return start_km, end_km, round((odo_end - odo_start) / 1000, 2), "odometer", missing
    distances = [_num(t["trip_distance"]) for t in day_trips if t.get("trip_distance") not in (None, "")]
    if distances:
        return start_km, end_km, round(sum(distances) / 1000, 2), "trip_distance", missing
    diffs = [max(0.0, _num(t["end_odometer"]) - _num(t["start_odometer"])) for t in day_trips if t.get("start_odometer") not in (None, "") and t.get("end_odometer") not in (None, "")]
    if diffs:
        return start_km, end_km, round(sum(diffs) / 1000, 2), "trip_odometer", missing
    return start_km, end_km, None, "none", missing


def split_idle(day_trips: list[dict], sites: list[tuple[float, float]]) -> tuple[int, int, int, int]:
    """(total, at_stop, elsewhere, trips_at_stop). Whole-trip rule: a trip that starts or ends near a site is 'at stop'."""
    total = at_stop = near_trips = 0
    for trip in day_trips:
        idle = _int(trip.get("idle_time_seconds"))
        total += idle
        if _near(trip.get("start_coordinates"), sites) or _near(trip.get("end_coordinates"), sites):
            at_stop += idle
            near_trips += 1
    return total, at_stop, total - at_stop, near_trips


def coverage(samples: list[dict], day: date) -> dict:
    """How well the 10-minute sampler covered this Manila day."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=config.MANILA)
    end = start + timedelta(days=1)
    stamps = sorted(s["ts"] for s in samples if start <= s["ts"] < end)
    if not stamps:
        return {"samples": 0, "partial_day": True, "max_gap_min": None}
    gap = timedelta(minutes=config.SAMPLE_MAX_GAP_MINUTES)
    points = [start] + stamps + [end]
    biggest = max(b - a for a, b in zip(points, points[1:]))
    return {"samples": len(stamps), "partial_day": biggest > gap, "max_gap_min": round(biggest.total_seconds() / 60)}


def battery(samples: list[dict]) -> dict:
    running = [s["vext"] for s in samples if s.get("ignition") and s.get("vext") is not None]
    parked = [s["vext"] for s in samples if not s.get("ignition") and s.get("vext") is not None]
    running_avg = round(sum(running) / len(running), 2) if running else None
    parked_min = round(min(parked), 2) if parked else None
    basis = running_avg if running_avg is not None else (round(sum(parked) / len(parked), 2) if parked else None)
    system = None if basis is None else (24 if basis > config.ELECTRICAL_24V_ABOVE_VOLTS else 12)
    return {"vext_parked_min": parked_min, "vext_running_avg": running_avg, "electrical_system": system}


def fuel(samples: list[dict], capacity_l: float | None) -> dict:
    """Fuel % start/end plus refuel and parked-drop estimates (analog sensor = estimate)."""
    ordered = sorted((s for s in samples if s.get("fuel_pct") is not None), key=lambda s: s["ts"])
    if not ordered:
        return {"fuel_pct_start": None, "fuel_pct_end": None, "refuel_events": None, "refuel_litres_est": None, "parked_drop_litres_est": None}
    refuels = 0
    refuel_points = 0.0
    for before, after in zip(ordered, ordered[1:]):
        rise = after["fuel_pct"] - before["fuel_pct"]
        if rise > config.REFUEL_MIN_RISE_POINTS and (before.get("speed") or 0) == 0 and (after.get("speed") or 0) == 0:
            refuels += 1
            refuel_points += rise
    drop_points = 0.0
    i = 0
    window = timedelta(minutes=config.PARKED_DROP_WINDOW_MINUTES)
    while i < len(ordered) - 1:
        if ordered[i].get("ignition"):
            i += 1
            continue
        best_j, best_drop = None, 0.0
        for j in range(i + 1, len(ordered)):
            if ordered[j]["ts"] - ordered[i]["ts"] > window or ordered[j].get("ignition"):
                break
            drop = ordered[i]["fuel_pct"] - ordered[j]["fuel_pct"]
            if drop > config.PARKED_DROP_MIN_POINTS and drop > best_drop:
                best_j, best_drop = j, drop
        if best_j is not None:
            drop_points += best_drop
            i = best_j
        else:
            i += 1
    litres = (lambda points: round(points / 100.0 * capacity_l, 2)) if capacity_l else (lambda points: None)
    return {
        "fuel_pct_start": round(ordered[0]["fuel_pct"], 2),
        "fuel_pct_end": round(ordered[-1]["fuel_pct"], 2),
        "refuel_events": refuels,
        "refuel_litres_est": litres(refuel_points) if refuels else (0.0 if capacity_l else None),
        "parked_drop_litres_est": litres(drop_points) if drop_points else (0.0 if capacity_l else None),
    }


def compute_daily_row(*, vehicle: dict, day: date, trips: list[dict], samples: list[dict] | None, sites: list[tuple[float, float]], assignments: list[dict] | None = None, unresolved_delivery_points: int = 0) -> dict:
    """One vehicle_daily_stats row (plain dict). `samples is None` = no status samples exist for this day
    (e.g. a backfilled day): fuel/battery stay NULL and data_quality says why."""
    trips, duplicates = dedupe_trips(trips)
    day_trips = trips_of_day(trips, day)
    odo_start, odo_end, km, km_source, missing_distance = compute_distance(day_trips)
    total_idle, idle_stop, idle_else, near_trips = split_idle(day_trips, sites)
    engine = engine_time(day_trips)
    classified = bool(assignments)
    row = {
        "odometer_start_km": odo_start, "odometer_end_km": odo_end, "km_driven": km if km is not None else (0.0 if not day_trips else None),
        "trip_count": len(day_trips),
        "engine_seconds": engine["seconds"],
        # Without an assignment we do not know the delivery points, so the split is NOT guessed ("unclassified").
        "idle_seconds_total": total_idle, "idle_seconds_at_stop": idle_stop if classified else None, "idle_seconds_elsewhere": idle_else if classified else None,
        "speeding_events": sum(_int(t.get("road_speeding_events")) for t in day_trips),
        "speeding_seconds": sum(_int(t.get("road_speeding_duration_seconds")) for t in day_trips),
        "max_speed_kmh": max((_int(t.get("max_speed")) for t in day_trips), default=0),
        "harsh_braking": sum(_int(t.get("harsh_braking_events")) for t in day_trips),
        "harsh_acceleration": sum(_int(t.get("harsh_acceleration_events")) for t in day_trips),
        "harsh_cornering": sum(_int(t.get("harsh_cornering_events")) for t in day_trips),
    }
    quality: dict = {"km_source": km_source, "trips_missing_distance": missing_distance, "idle_method": "estimate from trip start/end within 200 m of Mets, Glacier or an assigned SO delivery point"}
    if classified:
        quality.update(idle_split="classified", idle_trips_at_stop=near_trips)
    else:
        quality.update(idle_split="unclassified", idle_split_note="no assignment data, idle not classified")
    if duplicates:
        quality["duplicate_trips_dropped"] = duplicates
    if engine["overlap_seconds"]:
        quality["overlapping_trip_seconds"] = engine["overlap_seconds"]
    if engine["capped"]:
        quality["engine_seconds_capped"] = {"raw_sum": engine["raw_sum"], "capped_to": engine["seconds"]}
    if unresolved_delivery_points:
        quality["delivery_points_unresolved"] = unresolved_delivery_points
    day_samples = None if samples is None else [s for s in samples if manila_day(s["ts"]) == day]
    if day_samples is None or not day_samples:
        row.update(vext_parked_min=None, vext_running_avg=None, electrical_system=None, fuel_pct_start=None, fuel_pct_end=None, refuel_events=None, refuel_litres_est=None, parked_drop_litres_est=None)
        quality.update(status_samples=0, partial_day=day_samples is not None, no_status_samples=True)
    else:
        cov = coverage(day_samples, day)
        row.update(battery(day_samples))
        row.update(fuel(day_samples, vehicle.get("fuel_capacity_l")))
        quality.update(status_samples=cov["samples"], partial_day=cov["partial_day"], max_gap_min=cov["max_gap_min"])
        if odo_start is None and day_samples:
            ordered = sorted(day_samples, key=lambda s: s["ts"])
            if ordered[0].get("odometer_m") is not None:
                row["odometer_start_km"] = round(ordered[0]["odometer_m"] / 1000, 2)
                row["odometer_end_km"] = round(ordered[-1]["odometer_m"] / 1000, 2)
    if vehicle.get("fuel_capacity_l"):
        quality["fuel_capacity_l"] = vehicle["fuel_capacity_l"]   # kept so the UI can sanity-check fuel-log fills without a Cartrack call
    else:
        quality["fuel_capacity_missing"] = True
    if vehicle.get("plate_key") in config.UNCONFIRMED_FUEL_CAPACITY_PLATES:
        quality["capacity_unconfirmed"] = True
    assignments = assignments or []
    drivers = [a.get("driver_id") for a in assignments if a.get("driver_id") is not None]
    driver_counts = Counter(drivers)
    if len(driver_counts) == 1:
        row["primary_staff_id"] = next(iter(driver_counts))
    elif len(driver_counts) > 1:
        row["primary_staff_id"] = None
        quality["ambiguous_driver_attribution"] = True
        quality["driver_candidates"] = sorted(driver_counts)
        quality["driver_attribution_note"] = "multiple assigned drivers on this vehicle-day; excluded from individual eco scores"
    else:
        row["primary_staff_id"] = None
    row["assigned"] = bool(assignments)
    row["data_quality"] = quality
    return row
