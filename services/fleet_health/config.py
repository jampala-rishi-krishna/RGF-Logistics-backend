"""Fleet Health constants. Everything tunable lives here (weights/thresholds for later phases are added
to this module too), so nothing is buried in the calculators."""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo

MANILA = ZoneInfo("Asia/Manila")

# Warehouses (idle at a warehouse = "at stop"). Coordinates from the spec.
WAREHOUSE_SITES = {"Mets": (14.2907776, 121.0134132), "Glacier": (14.4922771, 120.9929815)}
AT_STOP_RADIUS_M = 200.0

# Cartrack trips: rows per page via ?limit= (per_page/page_size are ignored; 100, 150, 200, 250 and 500 verified).
# 200 keeps pages near 250 KB: a 300 KB page once arrived truncated, so bigger pages were not worth the saved calls.
TRIPS_PAGE_LIMIT = int(os.environ.get("FLEET_HEALTH_TRIPS_LIMIT", "200"))

# Fuel (analog sensor = estimate only)
REFUEL_MIN_RISE_POINTS = 10.0          # fuel % rises more than this between samples while stationary
PARKED_DROP_MIN_POINTS = 5.0           # fuel % falls more than this (of capacity) ...
PARKED_DROP_WINDOW_MINUTES = 120       # ... within this window with ignition OFF -> "possible fuel drop, check"

# Battery: system voltage is inferred from the running average
ELECTRICAL_24V_ABOVE_VOLTS = 20.0

# Sampler: one /rest/vehicles/status call every SAMPLE_INTERVAL_SECONDS
SAMPLE_INTERVAL_SECONDS = 600
SAMPLE_MAX_GAP_MINUTES = 30            # a longer hole in a day's samples => data_quality.partial_day
SAMPLE_RETENTION_DAYS = 3

# DCD8953 / DCD8954 report a 200 L tank in Cartrack: logistics has not confirmed it. Every litre estimate for these
# trucks is marked "capacity unconfirmed" (data_quality + UI tooltip) until this list is emptied.
UNCONFIRMED_FUEL_CAPACITY_PLATES = {"DCD8953", "DCD8954"}

# Nightly job: 01:00 Manila, computes the previous Manila day.
NIGHTLY_HOUR = 1
NIGHTLY_MINUTE = 0

BACKFILL_DAYS_DEFAULT = 30


# ---------------------------------------------------------------------------------------------------------
# Phase B: maintenance
# ---------------------------------------------------------------------------------------------------------
SERVICE_TYPES = ("oil_change", "tires", "brakes", "reefer_service", "general_pms", "battery")
SERVICE_LABELS = {"oil_change": "Oil", "tires": "Tyres", "brakes": "Brakes", "reefer_service": "Reefer", "general_pms": "PMS", "battery": "Battery"}
SERVICE_DUE_SOON_FROM = 0.80           # usage 80-100% = Due soon
SERVICE_OVERDUE_ABOVE = 1.00           # usage > 100% = Overdue

# Battery (per day). Parked = ignition off, running = ignition on.
BATTERY = {
    12: {"parked_warn_below": 12.2, "parked_critical_below": 11.9, "running_ok": (13.5, 14.7)},
    24: {"parked_warn_below": 24.4, "parked_critical_below": 23.8, "running_ok": (27.0, 29.0)},
}
BATTERY_WARNING_DAYS_FOR_FLAG = 2      # 2 consecutive warning days -> flag; 1 critical day -> flag

# Truck risk score 0-100 (higher = needs attention). Every number is shown with its breakdown.
RISK_WEIGHTS = {
    "service_overdue_points": 40, "service_due_soon_points": 20,
    "flag_points": {"critical": 15, "warning": 5, "info": 0}, "flag_cap": 25,
    "failed_checklist_points": 5, "failed_checklist_cap": 15, "failed_checklist_days": 7,
    "overload_points": 2, "overload_cap": 10, "overload_days": 30,
    "repair_points": 5, "repair_cap": 10, "repair_days": 90,
}
RISK_BANDS = (("high", 60), ("medium", 30), ("low", 0))   # score >= threshold

SNAPSHOT_TTL_SECONDS = 300             # fleet-health summary/trucks cache (invalidated by any edit)

CHECKLIST_ITEMS = ("tires", "lights", "brakes", "leaks", "mirrors_wipers", "body_damage", "reefer_running", "documents")
CHECKLIST_VALUES = ("ok", "issue", "na")

# ---------------------------------------------------------------------------------------------------------
# Phase C: eco driving (weights are config; shown to the user next to every score)
# ---------------------------------------------------------------------------------------------------------
ECO_MIN_KM_PER_WEEK = 50.0
ECO_WEIGHTS = {
    "speeding_per_100km_seconds": 0.05, "speeding_cap": 30,      # penalty = speeding seconds per 100 km x 0.05
    "harsh_per_100km_events": 2.5, "harsh_cap": 25,               # penalty = harsh events per 100 km x 2.5
    "idle_elsewhere_min_per_driving_hour": 1.0, "idle_cap": 25,   # penalty = idle-elsewhere minutes per driving hour x 1.0
    "kmpl_below_average_pct": 0.5, "kmpl_cap": 20,                # penalty = % below the fleet average km/L x 0.5
}
CO2_KG_PER_LITRE_DIESEL = 2.68
KMPL_PLAUSIBLE_RANGE = (2.0, 18.0)     # outside this a full-to-full result is marked "check"
TANK_OVERFILL_FACTOR = 1.05            # a fill above capacity x 1.05 is marked "check"
SCORECARD_WEEKDAY = 0                  # Monday
SCORECARD_HOUR = 8
