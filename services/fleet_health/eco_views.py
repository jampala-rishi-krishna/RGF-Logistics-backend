"""Eco-driving views built from vehicle_daily_stats + fuel_logs rows (already loaded: no I/O here except the staff-name lookup)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from services import staff_directory_cache
from services.fleet_health import config, eco


def _iso(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def fills_by_vehicle(fuel_rows: list[dict]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in fuel_rows:
        grouped[row["vehicle_id"]].append(row)
    return grouped


def kmpl_intervals(fuel_rows: list[dict]) -> dict[int, list[dict]]:
    return {vehicle_id: eco.full_to_full(fills) for vehicle_id, fills in fills_by_vehicle(fuel_rows).items()}


def aggregate_kmpl(intervals: list[dict]) -> float | None:
    km = sum(i["km"] for i in intervals)
    litres = sum(i["litres"] for i in intervals)
    return round(km / litres, 2) if litres > 0 and km > 0 else None


def intervals_ending_between(intervals: list[dict], first: date, last: date) -> list[dict]:
    return [i for i in intervals if first <= i["end_at"].astimezone(config.MANILA).date() <= last]


def staff_name(staff_id) -> str:
    member = staff_directory_cache.get_by_id(staff_id, retry_on_miss=False) if staff_id is not None else None
    return (member or {}).get("name") or f"Driver #{staff_id}"


def driver_scores(daily_rows: list[dict], fuel_rows: list[dict], week_start: date, *, plate_of: dict[int, str]) -> list[dict]:
    """Scores for one Mon-Sun week, ranked, with the previous week's score for the trend."""
    intervals_by_vehicle = kmpl_intervals(fuel_rows)
    all_intervals = [i for items in intervals_by_vehicle.values() for i in items]
    week_end = week_start + timedelta(days=6)
    fleet_kmpl = aggregate_kmpl(intervals_ending_between(all_intervals, week_end - timedelta(days=29), week_end))

    def scores_for(start: date) -> dict[int, dict]:
        end = start + timedelta(days=6)
        by_driver: dict[int, list[dict]] = defaultdict(list)
        for row in daily_rows:
            quality = row.get("data_quality") or {}
            if (
                start <= row["stat_date"] <= end
                and row.get("assigned")
                and row.get("primary_staff_id") is not None
                and row.get("km_driven") is not None
                and not quality.get("ambiguous_driver_attribution")
            ):
                by_driver[row["primary_staff_id"]].append(row)
        result = {}
        for staff_id, rows in by_driver.items():
            trucks = {r["vehicle_id"] for r in rows}
            week_intervals = [i for v in trucks for i in intervals_ending_between(intervals_by_vehicle.get(v, []), start, end)]
            kmpl = aggregate_kmpl(week_intervals)
            scored = eco.driver_eco_score(rows, kmpl=kmpl, fleet_kmpl=fleet_kmpl)
            result[staff_id] = {**scored, "staff_id": staff_id, "trucks": sorted(plate_of.get(v, str(v)) for v in trucks), "kmpl": kmpl}
        return result

    current = scores_for(week_start)
    previous = scores_for(week_start - timedelta(days=7))
    leaderboard = []
    for staff_id, item in current.items():
        before = previous.get(staff_id, {}).get("score")
        item["name"] = staff_name(staff_id)
        item["band"] = eco.score_band(item["score"])
        item["previous_score"] = before
        item["trend"] = (item["score"] - before) if item["score"] is not None and before is not None else None
        leaderboard.append(item)
    leaderboard.sort(key=lambda d: (d["score"] is None, -(d["score"] or 0), d["name"]))
    rank = 0
    for item in leaderboard:
        if item["score"] is not None:
            rank += 1
            item["rank"] = rank
        else:
            item["rank"] = None
    return leaderboard


def truck_rollups(daily_rows: list[dict], fuel_rows: list[dict], first: date, last: date, *, vehicles: list[dict]) -> list[dict]:
    """Per tracked truck: km (assigned / unassigned), idle (classified only), speeding, harsh, km/L, litres, CO2."""
    intervals_by_vehicle = kmpl_intervals(fuel_rows)
    fills = fills_by_vehicle(fuel_rows)
    result = []
    for vehicle in vehicles:
        rows = [r for r in daily_rows if r["vehicle_id"] == vehicle["id"] and first <= r["stat_date"] <= last]
        totals = eco.rollup(rows) if rows else None
        in_period = [f for f in fills.get(vehicle["id"], []) if first <= f["filled_at"].astimezone(config.MANILA).date() <= last]
        litres = round(sum(float(f["litres"]) for f in in_period), 2) if in_period else None
        spend = round(sum(float(f["amount_php"]) for f in in_period), 2) if in_period else None
        period_intervals = intervals_ending_between(intervals_by_vehicle.get(vehicle["id"], []), first, last)
        result.append({
            "vehicle_id": vehicle["id"], "plate": vehicle["plate"], "totals": totals, "days_with_data": len(rows),
            "kmpl": aggregate_kmpl(period_intervals), "kmpl_intervals": len(period_intervals),
            "litres": litres, "spend_php": spend, "co2_kg": eco.co2_kg(litres),
            "capacity_unconfirmed": vehicle.get("capacity_unconfirmed", False),
        })
    return result


def sensor_fuel_checks(daily_rows: list[dict], *, plates: dict[int, str], unconfirmed: set[int]) -> list[dict]:
    """Sensor-based estimates (analog fuel gauge): refuels and parked drops. Always worded as an estimate / a check."""
    checks = []
    for row in daily_rows:
        note = " Tank capacity is unconfirmed for this truck." if row["vehicle_id"] in unconfirmed else ""
        if row.get("parked_drop_litres_est"):
            checks.append({"vehicle_id": row["vehicle_id"], "plate": plates.get(row["vehicle_id"]), "date": _iso(row["stat_date"]), "kind": "parked_drop", "litres_est": float(row["parked_drop_litres_est"]),
                           "text": f"Possible fuel drop of about {float(row['parked_drop_litres_est']):.0f} L while parked (estimate from the analog sensor), check.{note}"})
        if row.get("refuel_events"):
            checks.append({"vehicle_id": row["vehicle_id"], "plate": plates.get(row["vehicle_id"]), "date": _iso(row["stat_date"]), "kind": "refuel", "litres_est": float(row["refuel_litres_est"] or 0),
                           "text": f"{row['refuel_events']} refuel(s), about {float(row['refuel_litres_est'] or 0):.0f} L (estimate from the analog sensor).{note}"})
    return sorted(checks, key=lambda c: c["date"], reverse=True)


TIPS = {
    "speeding": "Bawasan ang bilis, sundin ang speed limit sa highway.",
    "harsh": "Dahan-dahan sa preno at liko, smooth lang ang pagpapatakbo.",
    "idle": "Iwasan ang matagal na idling, patayin ang makina kung naghihintay.",
    "kmpl": "I-check ang gulong at iwasan ang biglaang arangkada para tipid sa diesel.",
}


def scorecard_message(driver: dict) -> dict:
    """Short Taglish WhatsApp message: the score, the best thing and ONE tip (the biggest penalty)."""
    first = (driver.get("name") or "Driver").split(" ")[0]
    if driver["score"] is None:
        return {"text": None, "skip_reason": "Not enough data this week"}
    worst = max(driver["breakdown"], key=lambda b: b["penalty"]) if driver["breakdown"] else None
    tip = TIPS.get(worst["key"]) if worst and worst["penalty"] > 0 else "Ituloy lang ang maayos na pagmamaneho!"
    trend = driver.get("trend")
    trend_text = "" if trend is None else (f" (+{trend} vs last week)" if trend > 0 else f" ({trend} vs last week)" if trend < 0 else " (same as last week)")
    text = f"Hi {first}! Eco-driving score mo this week: {driver['score']}/100{trend_text}. {tip} - RGF Logistics"
    return {"text": text, "skip_reason": None}
