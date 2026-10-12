"""Builds realistic Fleet Health API responses (the REAL routers/services on an in-memory database) and writes them as JSON
fixtures next to this script. The Playwright screenshots mock the network with these. Run from backend/:

    venv/Scripts/python.exe scratch/ui/fleet-health/make_fixtures.py
"""
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, BACKEND)
os.environ.setdefault("JWT_SECRET", "fixtures")

from sqlalchemy import BigInteger, create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
from database import Base
from models.fleet_health import FuelLog, MaintenanceRecord, PretripChecklist, ServiceInterval, VehicleDailyStat, VehicleFlag
from models.vehicle import Vehicle
from services import vehicle_flags, whatsapp_control
from services.fleet_health import config, eco_views, scorecard, snapshot

MANILA = config.MANILA


@compiles(BigInteger, "sqlite")
def _bigint(type_, compiler, **kw):
    return "INTEGER"


random.seed(7)
STAFF = {13: ("Juan Dela Cruz", "0917 111 2222"), 7: ("Pedro Reyes", "0918 333 4444"), 21: ("Mark Santos", ""), 8: ("Ricky Bautista", "0919 555 6666")}
eco_views.staff_name = lambda sid: STAFF.get(sid, (f"Driver #{sid}", ""))[0]
scorecard.staff_directory_cache.get_by_id = lambda sid, **kw: ({"phone": STAFF[sid][1]} if sid in STAFF else None)

engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
Base.metadata.create_all(engine, tables=[m.__table__ for m in (Vehicle, VehicleDailyStat, ServiceInterval, MaintenanceRecord, VehicleFlag, PretripChecklist, FuelLog)])
today = datetime.now(MANILA).date()
db = sessionmaker(bind=engine)()

TRUCKS = {1: ("DCD8953", True), 2: ("DCD8954", True), 3: ("DCD8955", True), 4: ("NFX5791", False), 5: ("NAJ6018", None), 6: ("NAN9911", None)}
for vid, (plate, reefer) in TRUCKS.items():
    db.add(Vehicle(id=vid, plate_no=plate, is_gps_tracked=True, is_third_party=False, is_reefer=reefer, vehicle_type="Reefer truck" if reefer else "Truck"))
for i, name in enumerate(["ASIAN CONNECT", "ASIAN CONNECT 2", "ASIAN CONNECT 3", "INHOUSE RIDER", "WETMARKET", "BHENTZ"], 1):
    db.add(Vehicle(id=-91000 - i, plate_no=name, is_gps_tracked=False, is_third_party=True, vehicle_type="Third-party truck"))
db.add(Vehicle(id=7, plate_no="Motorcycle 1", is_gps_tracked=False, is_third_party=False, is_reefer=False, vehicle_type="Motorcycle"))
db.add(Vehicle(id=8, plate_no="Motorcycle 2", is_gps_tracked=False, is_third_party=False, is_reefer=False, vehicle_type="Motorcycle"))
for service_type, km, hours, days in (("oil_change", 10000, 250, 180), ("tires", 40000, None, 730), ("brakes", 30000, None, 365), ("reefer_service", None, 500, 180), ("general_pms", 20000, 500, 365), ("battery", None, None, 730)):
    db.add(ServiceInterval(vehicle_id=None, service_type=service_type, interval_km=km, interval_engine_hours=hours, interval_days=days, active=True, confirmed=False))
db.commit()

ODO = {1: 44750.0, 2: 52300.0, 3: 40850.0, 4: 193070.0, 5: 261400.0, 6: 199940.0}
# 60 days of daily rows (battery drifting down on NFX5791, a parked fuel drop on DCD8953)
for vid in TRUCKS:
    odo = ODO[vid]
    for offset in range(0, 60):
        day = today - timedelta(days=offset)
        weekday = day.weekday() < 6
        km = 0.0 if (vid == 5 and offset > 0) else (round(random.uniform(60, 280), 1) if weekday and vid in (3, 4) else round(random.uniform(20, 140), 1) if weekday and vid in (1, 2) else round(random.uniform(0, 12), 1) if vid == 6 else 0.0)
        assigned = km > 30 and vid in (1, 2, 3, 4) and random.random() > 0.2
        staff = {1: 13, 2: 7, 3: 8, 4: 13}.get(vid) if assigned else None
        engine_s = int(km * 100 + random.randint(1800, 9000)) if km else 0
        idle = int(random.uniform(300, 4000)) if km else 0
        at_stop = int(idle * random.uniform(0.2, 0.8)) if assigned else None
        parked = round(12.75 - (offset * -0.012 if vid == 4 else 0) - (0.55 if vid == 4 and offset < 3 else 0) + random.uniform(-0.12, 0.1), 2) if vid in (1, 2, 3, 4, 6) else None
        db.add(VehicleDailyStat(
            vehicle_id=vid, stat_date=day, odometer_end_km=round(odo - sum(0 for _ in range(1)) - offset * 55, 1), km_driven=km, trip_count=int(km / 12) + 1 if km else 0, engine_seconds=engine_s, idle_seconds_total=idle,
            idle_seconds_at_stop=at_stop, idle_seconds_elsewhere=(idle - at_stop) if assigned else None, speeding_events=int(km / 25) if km else 0, speeding_seconds=int(km * random.uniform(0.5, 6)) if km else 0,
            max_speed_kmh=int(random.uniform(55, 104)) if km else 0, harsh_braking=random.choice([0, 0, 0, 1]) if km else 0, harsh_acceleration=random.choice([0, 0, 0, 0, 1]) if km else 0, harsh_cornering=random.choice([0, 0, 1, 2]) if km else 0,
            vext_parked_min=parked if offset < 25 else None, vext_running_avg=round(random.uniform(13.7, 14.4), 2) if (offset < 25 and vid != 5) else None, electrical_system=12 if (offset < 25 and vid != 5) else None,
            fuel_pct_start=round(random.uniform(40, 90), 1) if offset < 25 else None, fuel_pct_end=round(random.uniform(20, 80), 1) if offset < 25 else None, refuel_events=1 if (offset in (4, 11) and vid == 3) else 0 if offset < 25 else None,
            refuel_litres_est=55.0 if (offset in (4, 11) and vid == 3) else (0.0 if offset < 25 else None), parked_drop_litres_est=18.0 if (vid == 1 and offset == 2) else (0.0 if offset < 25 else None),
            primary_staff_id=staff, assigned=assigned, data_quality={"fuel_capacity_l": {1: 200.0, 2: 200.0, 3: 100.0, 4: 55.0, 5: 75.0, 6: 55.0}[vid], "idle_split": "classified" if assigned else "unclassified", "partial_day": offset == 6}))
db.commit()

def record(vid, kind, service_type, days_ago, odo_km=None, **kw):
    db.add(MaintenanceRecord(vehicle_id=vid, kind=kind, service_type=service_type, performed_on=today - timedelta(days=days_ago), odometer_km=odo_km, **kw))

record(1, "service", "oil_change", 40, 44100.0, vendor="Casa Mitsubishi", cost_php=6800)
record(1, "service", "general_pms", 90, 43000.0, vendor="Casa Mitsubishi", cost_php=18500)
record(1, "service", "tires", 200, 36000.0)
record(1, "service", "brakes", 120, 41000.0)
record(1, "service", "reefer_service", 60, None, engine_hours=1440)
record(2, "service", "oil_change", 25, 51900.0, vendor="Casa Mitsubishi", cost_php=6800)
record(3, "service", "oil_change", 150, 31200.0, vendor="Autohub", cost_php=7200)            # km since: 9,650 -> 96.5% -> due soon
record(3, "service", "brakes", 20, 40100.0, vendor="Autohub", cost_php=14400)
record(4, "service", "oil_change", 210, 170000.0, vendor="Isuzu Pasig", cost_php=5400)       # overdue
record(4, "service", "tires", 60, 188000.0)
record(5, "repair", None, 12, 261000.0, downtime_start=datetime.now(MANILA) - timedelta(days=12), reason="Transmission rebuild", vendor="TransCare", cost_php=86000, notes="Awaiting parts")
record(6, "service", "oil_change", 30, 199500.0)
record(4, "repair", None, 70, 191000.0, downtime_start=datetime.now(MANILA) - timedelta(days=71), downtime_end=datetime.now(MANILA) - timedelta(days=68), reason="Radiator hose", vendor="Isuzu Pasig", cost_php=4300)
db.commit()

vehicle_flags.report_issue(4, "manual", "warning", "Left mirror loose", reported_by="Rishi", db=db)
vehicle_flags.report_issue(4, "email", "critical", "Juan Dela Cruz reported by email: Engine is overheating near Calamba, need to stop", ref="email:18f3c", reported_by="Juan Dela Cruz", db=db)
vehicle_flags.report_issue(3, "battery", "warning", "Battery warning for 2 days in a row (latest %s): parked 12.12 V is below the warning limit 12.2 V (12 V system). Check battery/charging." % today, ref="daily:%s" % today, reported_by="Fleet Health", db=db)
vehicle_flags.report_issue(1, "fuel", "warning", "Possible fuel drop of about 18 L while parked on %s (estimate from the analog fuel sensor), check. Tank capacity is unconfirmed for this truck, so the litres are less certain." % (today - timedelta(days=2)), reported_by="Fleet Health", db=db)
vehicle_flags.report_issue(2, "overload", "warning", "Over capacity on %s: 2,140 kg planned on a 1,800 kg truck (+340 kg)" % today, ref="SO-18322, SO-18331", reported_by="Dispatch", db=db)
vehicle_flags.report_issue(6, "checklist", "warning", "Pre-trip check failed: tires.", ref="checklist:3", reported_by="Warehouse", db=db)
resolved = vehicle_flags.report_issue(2, "manual", "info", "Wiper blade replaced", reported_by="Rishi", db=db)
vehicle_flags.resolve_flag(db, resolved["flag"]["id"], 1, "Done in the yard")
db.commit()

ALL_OK = {k: "ok" for k in config.CHECKLIST_ITEMS}
for vid, offsets, bad in ((1, (0, 1, 2, 3), None), (2, (0, 1, 3), None), (3, (0, 2), "mirrors_wipers"), (4, (1, 2), None), (6, (0,), "tires")):
    for offset in offsets:
        items = dict(ALL_OK)
        if bad and offset == 0:
            items[bad] = "issue"
        if vid == 4:
            items["reefer_running"] = "na"
        db.add(PretripChecklist(vehicle_id=vid, staff_id=13, checked_at=datetime.now(timezone.utc) - timedelta(days=offset, hours=5), entered_by=1, items=items, reefer_temp_c=-18.4 if vid in (1, 2, 3) else None, passed=not (bad and offset == 0), notes="Left mirror needs tightening" if bad == "mirrors_wipers" and offset == 0 else None))
db.commit()

def fuel(vid, days_ago, litres, odo, full=True, station="Shell Carmona", price=61.5):
    db.add(FuelLog(vehicle_id=vid, staff_id=13, filled_at=datetime.now(timezone.utc) - timedelta(days=days_ago), litres=litres, amount_php=round(litres * price, 2), odometer_km=odo, full_tank=full, station=station))

for vid, base, step, per in ((4, 192100.0, 6, 5.6), (3, 40100.0, 7, 4.6), (1, 44100.0, 9, 5.2), (2, 51700.0, 8, 4.9)):
    odo = base
    for index in range(5):
        km = random.uniform(380, 520)
        litres = km / per * random.uniform(0.94, 1.08)
        fuel(vid, 40 - index * step, round(litres, 1), round(odo + km * index, 1), station=random.choice(["Shell Carmona", "Petron Sucat", "Caltex Silang"]), price=round(random.uniform(58, 64), 2))
fuel(1, 3, 220.0, 44500.0, full=False)           # 220 L on a 200 L tank (capacity unconfirmed): a "check"
db.commit()

import routers.fleet_health as fh
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auth.dependencies import CurrentUser, get_current_user
from database import get_db

app = FastAPI()
app.include_router(fh.router)
app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=1, email="rishi@x.com", role="admin", full_name="Rishi", status="active")
api = TestClient(app)
whatsapp_control.reset_to_default()
snapshot.invalidate()

out = os.path.join(HERE, "fixtures")
os.makedirs(out, exist_ok=True)

def save(name, path):
    response = api.get(path)
    assert response.status_code == 200, (path, response.status_code, response.text[:200])
    with open(os.path.join(out, name + ".json"), "w", encoding="utf-8") as fh_:
        json.dump(response.json(), fh_, indent=1)
    return response.json()

summary = save("summary", "/api/fleet-health/summary")
trucks = save("trucks", "/api/fleet-health/trucks")
for vid in (4, 1, 5, -91001, 7):
    save(f"truck_{vid}", f"/api/fleet-health/trucks/{vid}")
save("eco_drivers", "/api/fleet-health/eco/drivers")
save("eco_drivers_prev", "/api/fleet-health/eco/drivers?week=%s" % (today - timedelta(days=14)).isoformat())
save("eco_trucks", "/api/fleet-health/eco/trucks")
save("eco_fuel_checks", "/api/fleet-health/eco/fuel-checks")
save("eco_co2", "/api/fleet-health/eco/co2")
save("fuel_logs", "/api/fleet-health/fuel-logs")
save("scorecard", "/api/fleet-health/eco/scorecard")
print("KPIs:", json.dumps(summary["kpis"]))
print("needs attention:", [(t["plate"], t["risk"]["score"], t["risk"]["band"]) for t in summary["needs_attention"]])
print("written to", out, sorted(os.listdir(out)))
