"""Pure eco-driving calculators (no I/O): driver weekly score, truck rollups, full-to-full km/L, CO2, fuel checks.

Driver eco score (weekly, 0-100). Needs >= 50 km driven that week while assigned, else "Not enough data".
  start 100, subtract
    speeding  = speeding seconds per 100 km  x 0.05                (cap 30)
    harsh     = harsh braking+acceleration+cornering per 100 km x 2.5   (cap 25)
    idle      = idle-ELSEWHERE minutes per driving hour x 1.0      (cap 25)   idle at stops is shown, never penalised (reefer cooling)
    km/L      = % below the fleet-average km/L x 0.5               (cap 20)   only when that truck has fuel logs that week
A day belongs to the truck's PRIMARY driver of that day; days with no assignment are "unassigned": they stay in truck
stats but never count towards (or against) any driver.
km/L is full-to-full ONLY: km between two full-tank fills / litres added since the first one. The analog sensor is never the km/L source.
CO2 (kg) = diesel litres from the fuel log x 2.68.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from services.fleet_health import config

W = config.ECO_WEIGHTS


def _f(value, default=0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


# ---------------------------------------------------------------------------------------------------------
# km/L, fuel checks, CO2
# ---------------------------------------------------------------------------------------------------------
def full_to_full(fills: list[dict]) -> list[dict]:
    """fills (one truck): [{filled_at, litres, odometer_km, full_tank}]. Returns intervals between consecutive FULL fills:
    {start_at, end_at, km, litres, kmpl} where litres = everything added AFTER the first full fill up to and including
    the second one. Skips an interval without both odometers or with non-positive km/litres."""
    ordered = sorted(fills, key=lambda f: f["filled_at"])
    intervals = []
    anchor_index = None
    for index, fill in enumerate(ordered):
        if not fill.get("full_tank"):
            continue
        if anchor_index is not None:
            first = ordered[anchor_index]
            if first.get("odometer_km") is not None and fill.get("odometer_km") is not None:
                km = _f(fill["odometer_km"]) - _f(first["odometer_km"])
                litres = sum(_f(f["litres"]) for f in ordered[anchor_index + 1: index + 1])
                if km > 0 and litres > 0:
                    intervals.append({"start_at": first["filled_at"], "end_at": fill["filled_at"], "km": round(km, 1), "litres": round(litres, 2), "kmpl": round(km / litres, 2)})
        anchor_index = index
    return intervals


def fuel_log_checks(fills: list[dict], capacity_l: float | None, capacity_unconfirmed: bool = False) -> list[dict]:
    """Fills that look wrong, worded as a check."""
    checks = []
    note = " (tank capacity unconfirmed)" if capacity_unconfirmed else ""
    for fill in fills:
        if capacity_l and _f(fill.get("litres")) > capacity_l * config.TANK_OVERFILL_FACTOR:
            checks.append({"fill_id": fill.get("id"), "filled_at": fill["filled_at"], "reason": f"{_f(fill['litres']):.0f} L is more than the {capacity_l:.0f} L tank{note}, check"})
    low, high = config.KMPL_PLAUSIBLE_RANGE
    for interval in full_to_full(fills):
        if not (low <= interval["kmpl"] <= high):
            checks.append({"fill_id": None, "filled_at": interval["end_at"], "reason": f"{interval['kmpl']} km/L over {interval['km']:.0f} km looks unusual (expected {low}-{high}), check odometer or litres"})
    return checks


def co2_kg(litres: float | None) -> float | None:
    return None if litres is None else round(litres * config.CO2_KG_PER_LITRE_DIESEL, 1)


# ---------------------------------------------------------------------------------------------------------
# Rollups from vehicle_daily_stats rows
# ---------------------------------------------------------------------------------------------------------
def rollup(rows: list[dict]) -> dict:
    """Totals for a set of daily rows (any truck/driver/period)."""
    total_km = sum(_f(r.get("km_driven")) for r in rows)
    assigned = [r for r in rows if r.get("assigned")]
    classified_idle = [r for r in assigned if r.get("idle_seconds_elsewhere") is not None]
    engine = sum(_f(r.get("engine_seconds")) for r in rows)
    idle_total = sum(_f(r.get("idle_seconds_total")) for r in rows)
    return {
        "km": round(total_km, 1),
        "assigned_km": round(sum(_f(r.get("km_driven")) for r in assigned), 1),
        "unassigned_km": round(sum(_f(r.get("km_driven")) for r in rows if not r.get("assigned")), 1),
        "engine_hours": round(engine / 3600, 2),
        "driving_hours": round(max(0.0, engine - idle_total) / 3600, 2),
        "speeding_events": int(sum(_f(r.get("speeding_events")) for r in rows)),
        "speeding_seconds": int(sum(_f(r.get("speeding_seconds")) for r in rows)),
        "harsh_events": int(sum(_f(r.get("harsh_braking")) + _f(r.get("harsh_acceleration")) + _f(r.get("harsh_cornering")) for r in rows)),
        "idle_total_min": round(idle_total / 60, 1),
        "idle_at_stop_min": round(sum(_f(r.get("idle_seconds_at_stop")) for r in classified_idle) / 60, 1),
        "idle_elsewhere_min": round(sum(_f(r.get("idle_seconds_elsewhere")) for r in classified_idle) / 60, 1),
        "unclassified_idle_min": round(sum(_f(r.get("idle_seconds_total")) for r in rows if r.get("idle_seconds_elsewhere") is None) / 60, 1),
        "max_speed_kmh": max((int(_f(r.get("max_speed_kmh"))) for r in rows), default=0),
        "days": len(rows),
    }


def driver_eco_score(rows: list[dict], *, kmpl: float | None = None, fleet_kmpl: float | None = None) -> dict:
    """rows: that driver's ASSIGNED daily rows for one week."""
    totals = rollup(rows)
    km = totals["assigned_km"]
    if km < config.ECO_MIN_KM_PER_WEEK:
        return {"score": None, "status": "not_enough_data", "reason": f"needs at least {config.ECO_MIN_KM_PER_WEEK:.0f} km in the week (has {km:.0f} km)", "totals": totals, "breakdown": []}
    per100 = 100.0 / km
    speeding_raw = totals["speeding_seconds"] * per100 * W["speeding_per_100km_seconds"]
    harsh_raw = totals["harsh_events"] * per100 * W["harsh_per_100km_events"]
    idle_raw = (totals["idle_elsewhere_min"] / totals["driving_hours"] * W["idle_elsewhere_min_per_driving_hour"]) if totals["driving_hours"] > 0 else 0.0
    kmpl_raw = 0.0
    kmpl_detail = "no fuel logs for this truck this week: not scored"
    if kmpl is not None and fleet_kmpl and fleet_kmpl > 0:
        below = max(0.0, (fleet_kmpl - kmpl) / fleet_kmpl * 100)
        kmpl_raw = below * W["kmpl_below_average_pct"]
        kmpl_detail = f"{kmpl:.2f} km/L vs fleet average {fleet_kmpl:.2f} km/L ({below:.0f}% below)"
    parts = [
        ("speeding", "Speeding", speeding_raw, W["speeding_cap"], f"{totals['speeding_seconds']} s over the limit in {km:.0f} km = {totals['speeding_seconds'] * per100:.0f} s per 100 km", "seconds per 100 km x 0.05, capped at 30"),
        ("harsh", "Harsh driving", harsh_raw, W["harsh_cap"], f"{totals['harsh_events']} harsh event(s) in {km:.0f} km = {totals['harsh_events'] * per100:.1f} per 100 km", "events per 100 km x 2.5, capped at 25"),
        ("idle", "Idling away from stops", idle_raw, W["idle_cap"], f"{totals['idle_elsewhere_min']:.0f} min idle elsewhere in {totals['driving_hours']:.1f} driving h (idle at stops {totals['idle_at_stop_min']:.0f} min is not penalised)", "idle-elsewhere minutes per driving hour x 1.0, capped at 25"),
        ("kmpl", "Fuel economy", kmpl_raw, W["kmpl_cap"], kmpl_detail, "% below fleet-average km/L x 0.5, capped at 20"),
    ]
    breakdown, score = [], 100.0
    for key, label, raw, cap, detail, formula in parts:
        penalty = min(raw, cap)
        score -= penalty
        breakdown.append({"key": key, "label": label, "penalty": round(penalty, 1), "cap": cap, "detail": detail, "formula": formula})
    return {"score": max(0, round(score)), "status": "scored", "reason": None, "totals": totals, "breakdown": breakdown}


def score_band(score: int | None) -> str:
    if score is None:
        return "none"
    return "great" if score >= 85 else "good" if score >= 70 else "watch" if score >= 50 else "poor"
