"""Pure maintenance calculators (no I/O): service status, battery day rules, truck risk score.

Service usage  = max(km since last service / interval_km, engine hours since / interval_engine_hours, days since / interval_days)
                 OK < 80%  |  Due soon 80-100%  |  Overdue > 100%  |  No record (never serviced in the system: never guessed)
Battery day    = critical if parked minimum is below the critical limit; warning if below the warning limit or if the
                 running (charging) average is outside the normal range ("check alternator/charging"). 12 V vs 24 V is
                 inferred from the running average (> 20 V = 24 V system). A flag needs 2 consecutive warning days, or 1 critical day.
Risk score     = service (0-40) + non-checklist open flags (cap 25) + failed checklists 7 d (cap 15) + overloads 30 d (cap 10) + repairs 90 d (cap 10)
                 Low 0-29 | Medium 30-59 | High 60+ ; "Needs attention this week" = High, or any service Overdue.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from services.fleet_health import config

W = config.RISK_WEIGHTS


# ---------------------------------------------------------------------------------------------------------
# Service intervals
# ---------------------------------------------------------------------------------------------------------
def effective_interval(service_type: str, truck_overrides: dict, fleet_defaults: dict) -> dict | None:
    """Per-truck override beats the fleet default. Returns None when neither exists or the type is inactive."""
    interval = truck_overrides.get(service_type) or fleet_defaults.get(service_type)
    if not interval or not interval.get("active", True):
        return None
    return interval


def service_status(*, interval: dict | None, last_record: dict | None, odometer_km: float | None, engine_hours_since: float | None, today: date) -> dict:
    """One truck x one service type. `last_record`: {performed_on: date, odometer_km: float|None}."""
    if interval is None:
        return {"status": "not_tracked", "usage_pct": None, "components": {}}
    if last_record is None:
        return {"status": "no_record", "usage_pct": None, "components": {}, "interval": _interval_view(interval), "note": "never serviced in Fleet Health"}
    components: dict[str, dict] = {}
    raw: dict[str, float] = {}   # unrounded usage, so a band is never decided on a rounded number (79.99% stays "OK")
    if interval.get("interval_km") and odometer_km is not None and last_record.get("odometer_km") is not None:
        km_since = max(0.0, odometer_km - float(last_record["odometer_km"]))
        raw["km"] = km_since / interval["interval_km"] * 100
        components["km"] = {"since": round(km_since, 1), "interval": interval["interval_km"], "pct": round(raw["km"], 1)}
    if interval.get("interval_engine_hours") and engine_hours_since is not None:
        raw["engine_hours"] = engine_hours_since / interval["interval_engine_hours"] * 100
        components["engine_hours"] = {"since": round(engine_hours_since, 1), "interval": interval["interval_engine_hours"], "pct": round(raw["engine_hours"], 1)}
    if interval.get("interval_days"):
        days_since = max(0, (today - last_record["performed_on"]).days)
        raw["days"] = days_since / interval["interval_days"] * 100
        components["days"] = {"since": days_since, "interval": interval["interval_days"], "pct": round(raw["days"], 1)}
    if not components:
        return {"status": "no_record", "usage_pct": None, "components": {}, "interval": _interval_view(interval), "note": "service exists, but usage data is missing"}
    driver = max(raw, key=raw.get)
    usage = raw[driver]
    if usage > config.SERVICE_OVERDUE_ABOVE * 100:
        status = "overdue"
    elif usage >= config.SERVICE_DUE_SOON_FROM * 100:
        status = "due_soon"
    else:
        status = "ok"
    return {"status": status, "usage_pct": round(usage, 1), "driven_by": driver, "components": components, "interval": _interval_view(interval), "last_service_on": last_record["performed_on"].isoformat()}


def _interval_view(interval: dict) -> dict:
    return {"km": interval.get("interval_km"), "engine_hours": interval.get("interval_engine_hours"), "days": interval.get("interval_days"), "confirmed": bool(interval.get("confirmed")), "scope": "truck" if interval.get("vehicle_id") else "fleet"}


# ---------------------------------------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------------------------------------
def battery_day_status(row: dict) -> dict:
    """row: {vext_parked_min, vext_running_avg, electrical_system}. status: no_data | ok | warning | critical."""
    system = row.get("electrical_system")
    parked, running = row.get("vext_parked_min"), row.get("vext_running_avg")
    if system not in config.BATTERY or (parked is None and running is None):
        return {"status": "no_data", "reasons": []}
    limits = config.BATTERY[system]
    reasons, status = [], "ok"
    if parked is not None:
        if parked < limits["parked_critical_below"]:
            status = "critical"
            reasons.append(f"parked {parked:.2f} V is below the critical limit {limits['parked_critical_below']} V ({system} V system)")
        elif parked < limits["parked_warn_below"]:
            status = "warning"
            reasons.append(f"parked {parked:.2f} V is below the warning limit {limits['parked_warn_below']} V ({system} V system)")
    low, high = limits["running_ok"]
    if running is not None and not (low <= running <= high):
        if status == "ok":
            status = "warning"
        reasons.append(f"running {running:.2f} V is outside {low}-{high} V: check alternator/charging")
    return {"status": status, "reasons": reasons}


def battery_flag_decision(days: list[dict]) -> dict | None:
    """days: oldest -> newest [{date, status, reasons}] of CONSECUTIVE calendar days (a day with no data breaks the streak).
    Returns {"severity", "message"} when a flag is due: 1 critical day, or 2 consecutive warning(-or-worse) days."""
    if not days:
        return None
    latest = days[-1]
    if latest["status"] == "critical":
        return {"severity": "critical", "message": f"Battery critical on {latest['date']}: {'; '.join(latest['reasons'])}. Check battery/alternator."}
    streak = 0
    for day in reversed(days):
        if day["status"] in ("warning", "critical"):
            streak += 1
        else:
            break
    if streak >= config.BATTERY_WARNING_DAYS_FOR_FLAG:
        return {"severity": "warning", "message": f"Battery warning for {streak} days in a row (latest {latest['date']}): {'; '.join(latest['reasons'])}. Check battery/charging."}
    return None


def consecutive_tail(daily_rows: list[dict]) -> list[dict]:
    """Newest streak of consecutive calendar days that HAVE battery data, oldest -> newest, with status computed."""
    dated = sorted(({"date": r["stat_date"], **battery_day_status(r)} for r in daily_rows), key=lambda d: d["date"])
    tail: list[dict] = []
    for item in reversed(dated):
        if item["status"] == "no_data":
            break
        if tail and (tail[-1]["date"] - item["date"]).days != 1:
            break
        tail.append(item)
    return list(reversed(tail))


# ---------------------------------------------------------------------------------------------------------
# Risk score
# ---------------------------------------------------------------------------------------------------------
def risk_score(*, service_statuses: dict[str, dict], open_flags: list[dict], failed_checklists_7d: int, overloads_30d: int, repairs_90d: int) -> dict:
    worst = max((s.get("usage_pct") or 0 for s in service_statuses.values()), default=0)
    overdue = [t for t, s in service_statuses.items() if s["status"] == "overdue"]
    if worst >= config.SERVICE_OVERDUE_ABOVE * 100 or overdue:
        service_pts = W["service_overdue_points"]
    elif worst >= config.SERVICE_DUE_SOON_FROM * 100:
        service_pts = W["service_due_soon_points"]
    else:
        service_pts = 0
    scorable_flags = [f for f in open_flags if f.get("source") != "checklist"]
    flag_raw = sum(W["flag_points"].get(f["severity"], 0) for f in scorable_flags)
    flag_pts = min(flag_raw, W["flag_cap"])
    checklist_pts = min(failed_checklists_7d * W["failed_checklist_points"], W["failed_checklist_cap"])
    overload_pts = min(overloads_30d * W["overload_points"], W["overload_cap"])
    repair_pts = min(repairs_90d * W["repair_points"], W["repair_cap"])
    total = min(100, service_pts + flag_pts + checklist_pts + overload_pts + repair_pts)
    band = next(name for name, floor in config.RISK_BANDS if total >= floor)
    crit = sum(1 for f in scorable_flags if f["severity"] == "critical")
    warn = sum(1 for f in scorable_flags if f["severity"] == "warning")
    breakdown = [
        {"key": "service", "label": "Service", "points": service_pts, "max": W["service_overdue_points"], "detail": f"worst usage {worst:.0f}% ({len(overdue)} overdue)" if worst else "no service usage measured", "formula": "100%+ of any interval = 40, 80-100% = 20"},
        {"key": "flags", "label": "Open issues", "points": flag_pts, "max": W["flag_cap"], "detail": f"{crit} critical x {W['flag_points']['critical']} + {warn} warning x {W['flag_points']['warning']} (checklist-sourced issues are counted under failed checks)", "formula": "critical 15 each, warning 5 each, capped at 25; checklist flags are not double-counted"},
        {"key": "checklists", "label": "Failed pre-trip checks (7 d)", "points": checklist_pts, "max": W["failed_checklist_cap"], "detail": f"{failed_checklists_7d} failed", "formula": "5 each, capped at 15"},
        {"key": "overloads", "label": "Overloaded trips (30 d)", "points": overload_pts, "max": W["overload_cap"], "detail": f"{overloads_30d} flagged", "formula": "2 each, capped at 10"},
        {"key": "repairs", "label": "Repairs / downtime (90 d)", "points": repair_pts, "max": W["repair_cap"], "detail": f"{repairs_90d} repair record(s)", "formula": "5 each, capped at 10"},
    ]
    return {"score": total, "band": band, "breakdown": breakdown, "any_overdue": bool(overdue), "needs_attention": band == "high" or bool(overdue)}
