"""Automatic issue detection run after the nightly job writes a day: battery (2 warning days / 1 critical day) and fuel
(possible parked fuel drop). Everything goes through services.vehicle_flags.report_issue, so dedup and wording rules are shared.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.fleet_health import VehicleDailyStat
from services import vehicle_flags
from services.fleet_health import risk

logger = logging.getLogger("fleet_health.automation")


def evaluate_vehicle(db: Session, *, vehicle_id: int, plate: str, day: date, capacity_unconfirmed: bool = False) -> list[dict]:
    """Look at the last 7 days of one truck (rows must already be flushed) and raise battery/fuel flags for `day`."""
    rows = db.execute(select(VehicleDailyStat).where(VehicleDailyStat.vehicle_id == vehicle_id, VehicleDailyStat.stat_date >= day - timedelta(days=6), VehicleDailyStat.stat_date <= day).order_by(VehicleDailyStat.stat_date)).scalars().all()
    raised = []
    battery_rows = [{"stat_date": r.stat_date, "vext_parked_min": None if r.vext_parked_min is None else float(r.vext_parked_min), "vext_running_avg": None if r.vext_running_avg is None else float(r.vext_running_avg), "electrical_system": r.electrical_system} for r in rows]
    decision = risk.battery_flag_decision(risk.consecutive_tail(battery_rows))
    if decision:
        raised.append(vehicle_flags.report_issue(vehicle_id, "battery", decision["severity"], decision["message"], ref=f"daily:{day.isoformat()}", reported_by="Fleet Health", db=db))
    today_row = next((r for r in rows if r.stat_date == day), None)
    if today_row is not None and today_row.parked_drop_litres_est and float(today_row.parked_drop_litres_est) > 0:
        note = " Tank capacity is unconfirmed for this truck, so the litres are less certain." if capacity_unconfirmed else ""
        raised.append(vehicle_flags.report_issue(vehicle_id, "fuel", "warning", f"Possible fuel drop of about {float(today_row.parked_drop_litres_est):.0f} L while parked on {day.isoformat()} (estimate from the analog fuel sensor), check.{note}", ref=f"daily:{day.isoformat()}", reported_by="Fleet Health", db=db))
    return raised
