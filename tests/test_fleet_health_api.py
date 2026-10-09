"""Fleet Health phases B/C through the real routers, services and models on an in-memory SQLite database.
Cartrack, n8n and WhatsApp are never contacted; the production database is never opened (see tests/conftest.py)."""
import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("JWT_SECRET", "test")

from auth.dependencies import CurrentUser, get_current_user
from database import Base, get_db
from models.fleet_health import FuelLog, MaintenanceRecord, PretripChecklist, ServiceInterval, VehicleDailyStat, VehicleFlag
from models.vehicle import Vehicle
from routers import fleet_health
from services import vehicle_flags, whatsapp_control
from services.fleet_health import automation, config, eco_views, sampler, scorecard, snapshot
from services.vehicle_flags import IssueError

MANILA = config.MANILA


@compiles(BigInteger, "sqlite")
def _bigint(type_, compiler, **kw):
    return "INTEGER"


def today():
    return datetime.now(MANILA).date()


PLATES = {1: ("DCD8953", True), 2: ("DCD8954", True), 3: ("DCD8955", True), 4: ("NFX5791", False), 5: ("NAJ6018", None), 6: ("NAN9911", None)}


@pytest.fixture()
def engine():
    import models  # noqa: F401
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng, tables=[m.__table__ for m in (Vehicle, VehicleDailyStat, ServiceInterval, MaintenanceRecord, VehicleFlag, PretripChecklist, FuelLog)])
    with sessionmaker(bind=eng)() as s:
        for vid, (plate, reefer) in PLATES.items():
            s.add(Vehicle(id=vid, plate_no=plate, is_gps_tracked=True, is_third_party=False, is_reefer=reefer, vehicle_type="Truck"))
        s.add(Vehicle(id=7, plate_no="Motorcycle 1", is_gps_tracked=False, is_third_party=False, is_reefer=False, vehicle_type="Motorcycle"))
        s.add(Vehicle(id=-91001, plate_no="ASIAN CONNECT", is_gps_tracked=False, is_third_party=True, vehicle_type="Third-party truck"))
        for service_type, km, hours, days in (("oil_change", 10000, 250, 180), ("tires", 40000, None, 730), ("brakes", 30000, None, 365), ("reefer_service", None, 500, 180), ("general_pms", 20000, 500, 365), ("battery", None, None, 730)):
            s.add(ServiceInterval(vehicle_id=None, service_type=service_type, interval_km=km, interval_engine_hours=hours, interval_days=days, active=True, confirmed=False))
        s.commit()
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    with sessionmaker(bind=engine)() as session:
        yield session


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    snapshot.invalidate()
    sampler.reset()
    scorecard.reset()
    whatsapp_control.reset_to_default()
    monkeypatch.setattr(eco_views, "staff_name", lambda sid: {13: "Juan Dela Cruz", 7: "Pedro Reyes"}.get(sid, f"Driver #{sid}"))
    yield
    snapshot.invalidate()


def client(engine, role="admin", name="Pau"):
    app = FastAPI()
    app.include_router(fleet_health.router)
    factory = sessionmaker(bind=engine)

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=1, email=f"{name}@x.com", role=role, full_name=name, status="active")
    return TestClient(app)


def daily_row(vehicle_id, offset, **kw):
    base = dict(vehicle_id=vehicle_id, stat_date=today() - timedelta(days=offset), km_driven=100.0, trip_count=5, engine_seconds=18000, idle_seconds_total=1800, idle_seconds_at_stop=900, idle_seconds_elsewhere=900,
                speeding_events=0, speeding_seconds=0, max_speed_kmh=80, harsh_braking=0, harsh_acceleration=0, harsh_cornering=0, assigned=True, primary_staff_id=13, odometer_end_km=10000.0 - offset * 100,
                data_quality={"fuel_capacity_l": 100.0})
    return VehicleDailyStat(**{**base, **kw})


# =============================================================================================================== report_issue
def test_report_issue_validates_dedups_and_allows_reraise_after_resolve(db):
    first = vehicle_flags.report_issue("nfx 5791", "manual", "warning", "Brake noise", db=db)
    assert first["created"] is True and first["flag"]["vehicle_id"] == 4 and first["flag"]["source"] == "manual"
    again = vehicle_flags.report_issue(4, "manual", "warning", "  brake   NOISE ", db=db)
    assert again["created"] is False and again["flag"]["id"] == first["flag"]["id"]
    assert vehicle_flags.report_issue(4, "manual", "warning", "Tyre worn", db=db)["created"] is True
    assert vehicle_flags.report_issue(4, "voice", "warning", "Brake noise", db=db)["created"] is True        # a different source is a different issue
    vehicle_flags.resolve_flag(db, first["flag"]["id"], 1, "fixed")
    third = vehicle_flags.report_issue(4, "manual", "warning", "Brake noise", db=db)
    assert third["created"] is True and third["flag"]["id"] != first["flag"]["id"]               # dedup is among OPEN flags only


@pytest.mark.parametrize("kwargs,status", [
    (dict(source="nope", severity="warning", message="x"), 422), (dict(source="manual", severity="urgent", message="x"), 422),
    (dict(source="manual", severity="warning", message="   "), 422), (dict(source="manual", severity="warning", message="x" * 501), 422),
    (dict(source="manual", severity="warning", message="x", photo_ref="data:image/png;base64,AAAA"), 422),
    (dict(source="manual", severity="warning", message="x", occurred_at=datetime.now(timezone.utc) + timedelta(hours=2)), 422),
])
def test_report_issue_rejects_bad_input(db, kwargs, status):
    with pytest.raises(IssueError) as caught:
        vehicle_flags.report_issue(4, db=db, **kwargs)
    assert caught.value.status == status


def test_issues_for_third_party_or_unknown_vehicles_are_refused(db):
    with pytest.raises(IssueError) as caught:
        vehicle_flags.report_issue("ASIAN CONNECT", "manual", "info", "x", db=db)
    assert caught.value.status == 422 and "Third-party" in str(caught.value)
    with pytest.raises(IssueError) as caught:
        vehicle_flags.report_issue("ZZZ999", "manual", "info", "x", db=db)
    assert caught.value.status == 404


def test_driver_app_is_an_accepted_source_and_photo_is_only_a_reference(db):
    result = vehicle_flags.report_issue(4, "driver_app", "critical", "Smoke from engine", photo_ref="https://files.example/p/1.jpg", reported_by="driver_app:13", db=db)
    assert result["flag"]["photo_ref"] == "https://files.example/p/1.jpg" and result["flag"]["reported_by"] == "driver_app:13"
    assert "driver_app" in vehicle_flags.SOURCES


def test_issues_endpoint_contract(engine):
    api = client(engine, "dispatcher")
    created = api.post("/api/fleet-health/issues", json={"vehicle": "NFX5791", "source": "manual", "severity": "warning", "message": "Left mirror cracked", "ref": "SO-1", "photo_ref": "https://x/y.jpg"})
    assert created.status_code == 201 and created.json()["created"] is True and created.json()["flag"]["reported_by"] == "Pau"
    duplicate = api.post("/api/fleet-health/issues", json={"vehicle": 4, "source": "manual", "severity": "warning", "message": "Left mirror cracked"})
    assert duplicate.status_code == 200 and duplicate.json()["created"] is False
    assert api.post("/api/fleet-health/issues", json={"vehicle": 4, "source": "bad", "severity": "warning", "message": "x"}).status_code == 422
    assert api.post("/api/fleet-health/issues", json={"vehicle": "ASIAN CONNECT", "source": "manual", "severity": "info", "message": "x"}).status_code == 422
    assert api.post("/api/fleet-health/issues", json={"vehicle": 999, "source": "manual", "severity": "info", "message": "x"}).status_code == 404
    assert client(engine, "warehouse").post("/api/fleet-health/issues", json={"vehicle": 4, "source": "manual", "severity": "info", "message": "ok"}).status_code == 201
    assert client(engine, "driver").post("/api/fleet-health/issues", json={"vehicle": 4, "source": "manual", "severity": "info", "message": "no"}).status_code == 403


def test_resolve_flag_needs_edit_rights(engine, db):
    flag_id = vehicle_flags.report_issue(4, "manual", "warning", "Leak", db=db)["flag"]["id"]
    db.commit()
    assert client(engine, "warehouse").post(f"/api/fleet-health/flags/{flag_id}/resolve", json={"note": "x"}).status_code == 403
    resolved = client(engine, "planner").post(f"/api/fleet-health/flags/{flag_id}/resolve", json={"note": "Replaced hose"})   # planner = dispatcher
    assert resolved.status_code == 200 and resolved.json()["resolved_at"] and resolved.json()["resolution_note"] == "Replaced hose"
    assert client(engine).post("/api/fleet-health/flags/9999/resolve", json={}).status_code == 404


# =============================================================================================================== checklists
ALL_OK = {k: "ok" for k in config.CHECKLIST_ITEMS}


def test_failed_checklist_raises_a_flag_and_a_passed_one_does_not(engine, db):
    api = client(engine, "warehouse")
    ok = api.post("/api/fleet-health/pretrip-checklists", json={"vehicle_id": 4, "items": ALL_OK, "reefer_temp_c": -18.5})
    assert ok.status_code == 201 and ok.json()["passed"] is True and ok.json()["flag"] is None
    bad = api.post("/api/fleet-health/pretrip-checklists", json={"vehicle_id": 4, "items": {**ALL_OK, "brakes": "issue", "lights": "issue"}, "notes": "Soft pedal"})
    body = bad.json()
    assert bad.status_code == 201 and body["passed"] is False and body["flag"]["created"] is True
    assert body["flag"]["flag"]["source"] == "checklist" and body["flag"]["flag"]["severity"] == "critical" and "brakes" in body["flag"]["flag"]["message"]
    minor = api.post("/api/fleet-health/pretrip-checklists", json={"vehicle_id": 4, "items": {**ALL_OK, "mirrors_wipers": "issue"}}).json()
    assert minor["flag"]["flag"]["severity"] == "warning"
    assert api.post("/api/fleet-health/pretrip-checklists", json={"vehicle_id": 4, "items": {"bogus": "ok"}}).status_code == 422
    assert api.post("/api/fleet-health/pretrip-checklists", json={"vehicle_id": 4, "items": {"tires": "maybe"}}).status_code == 422
    assert api.post("/api/fleet-health/pretrip-checklists", json={"vehicle_id": -91001, "items": ALL_OK}).status_code == 422
    assert client(engine, "driver").post("/api/fleet-health/pretrip-checklists", json={"vehicle_id": 4, "items": ALL_OK}).status_code == 403


def test_omitted_checklist_items_default_to_na(engine):
    result = client(engine).post("/api/fleet-health/pretrip-checklists", json={"vehicle_id": 4, "items": {"tires": "ok"}}).json()
    assert result["items"]["tires"] == "ok" and result["items"]["reefer_running"] == "na" and result["passed"] is True


# =============================================================================================================== fuel logs
def test_fuel_log_crud_price_per_litre_and_roles(engine):
    api = client(engine, "warehouse")
    created = api.post("/api/fleet-health/fuel-logs", json={"vehicle_id": 4, "filled_at": (datetime.now(MANILA) - timedelta(days=1)).isoformat(), "litres": 40, "amount_php": 2400, "odometer_km": 1000, "full_tank": True, "station": "Shell"})
    assert created.status_code == 201 and created.json()["price_per_litre"] == 60.0
    log_id = created.json()["id"]
    assert api.put(f"/api/fleet-health/fuel-logs/{log_id}", json={"vehicle_id": 4, "filled_at": datetime.now(MANILA).isoformat(), "litres": 41, "amount_php": 2460}).status_code == 403   # warehouse: create only
    assert api.delete(f"/api/fleet-health/fuel-logs/{log_id}").status_code == 403
    boss = client(engine, "dispatcher")
    edited = boss.put(f"/api/fleet-health/fuel-logs/{log_id}", json={"vehicle_id": 4, "filled_at": (datetime.now(MANILA) - timedelta(days=1)).isoformat(), "litres": 41, "amount_php": 2460, "odometer_km": 1000, "full_tank": True})
    assert edited.status_code == 200 and edited.json()["litres"] == 41.0
    assert boss.delete(f"/api/fleet-health/fuel-logs/{log_id}").status_code == 204
    assert boss.delete(f"/api/fleet-health/fuel-logs/{log_id}").status_code == 404


@pytest.mark.parametrize("patch", [{"litres": 0}, {"litres": -3}, {"litres": 5000}, {"amount_php": -1}, {"filled_at": (datetime.now(MANILA) + timedelta(days=2)).isoformat()}, {"odometer_km": -5}, {"vehicle_id": -91001}, {"receipt_ref": "data:text/plain;base64,AA"}])
def test_fuel_log_validation(engine, patch):
    body = {"vehicle_id": 4, "filled_at": datetime.now(MANILA).isoformat(), "litres": 30, "amount_php": 1800, **patch}
    assert client(engine).post("/api/fleet-health/fuel-logs", json=body).status_code == 422


def test_fuel_log_list_shows_the_full_to_full_kmpl_and_checks(engine, db):
    now = datetime.now(timezone.utc)
    db.add_all([FuelLog(vehicle_id=3, filled_at=now - timedelta(days=9), litres=50, amount_php=3000, odometer_km=1000, full_tank=True),
                FuelLog(vehicle_id=3, filled_at=now - timedelta(days=2), litres=50, amount_php=3000, odometer_km=1500, full_tank=True),
                FuelLog(vehicle_id=3, filled_at=now - timedelta(days=1), litres=150, amount_php=9000, odometer_km=1520, full_tank=False)])
    db.add(daily_row(3, 1))
    db.commit()
    logs = client(engine).get("/api/fleet-health/fuel-logs").json()["logs"]
    by_litres = {l["litres"]: l for l in logs}
    assert by_litres[50.0]["price_per_litre"] == 60.0 and any(l["kmpl"] == 10.0 for l in logs)
    assert "more than the 100 L tank" in by_litres[150.0]["check"]


# =============================================================================================================== maintenance records, intervals, trucks
def record(vehicle_id=4, **kw):
    return {"vehicle_id": vehicle_id, "kind": "service", "service_type": "oil_change", "performed_on": (today() - timedelta(days=10)).isoformat(), "odometer_km": 9000, **kw}


def truck(api, vehicle_id):
    return next(t for t in api.get("/api/fleet-health/trucks").json()["trucks"] if t["id"] == vehicle_id)


def test_maintenance_record_crud_validation_and_roles(engine):
    boss = client(engine, "dispatcher")
    created = boss.post("/api/fleet-health/maintenance-records", json=record())
    assert created.status_code == 201
    record_id = created.json()["id"]
    assert boss.put(f"/api/fleet-health/maintenance-records/{record_id}", json=record(odometer_km=9100, vendor="Casa")).json()["vendor"] == "Casa"
    assert client(engine, "warehouse").post("/api/fleet-health/maintenance-records", json=record()).status_code == 403
    assert client(engine, "planner").post("/api/fleet-health/maintenance-records", json=record(service_type="tires")).status_code == 201
    bad = [record(kind="oops"), record(service_type=None), record(service_type="wings"), record(performed_on=(today() + timedelta(days=3)).isoformat()), record(performed_on="soon"),
           record(cost_php=-5), record(vehicle_id=-91001), record(vehicle_id=999),
           record(kind="repair", downtime_end="2026-01-02T00:00:00+08:00"), record(kind="repair", downtime_start="2026-01-03T00:00:00+08:00", downtime_end="2026-01-02T00:00:00+08:00"), record(receipt_ref="data:application/pdf;base64,AA")]
    assert [boss.post("/api/fleet-health/maintenance-records", json=b).status_code for b in bad] == [422, 422, 422, 422, 422, 422, 422, 404, 422, 422, 422]
    assert boss.delete(f"/api/fleet-health/maintenance-records/{record_id}").status_code == 204
    assert boss.delete(f"/api/fleet-health/maintenance-records/{record_id}").status_code == 404


def test_repair_record_ignores_service_type_and_keeps_downtime(engine):
    boss = client(engine)
    result = boss.post("/api/fleet-health/maintenance-records", json={"vehicle_id": 4, "kind": "repair", "service_type": "oil_change", "performed_on": today().isoformat(), "downtime_start": "2026-10-01T08:00:00+08:00", "reason": "Gearbox"}).json()
    assert result["service_type"] is None and result["downtime_start"].startswith("2026-10-01") and result["downtime_end"] is None


def test_services_show_ok_due_soon_overdue_and_no_record(engine, db):
    db.add(daily_row(4, 0, odometer_end_km=9500.0))
    db.commit()
    api = client(engine)
    assert truck(api, 4)["services"]["oil_change"]["status"] == "no_record"            # never serviced in the system: no guess
    api.post("/api/fleet-health/maintenance-records", json=record(odometer_km=8000))                              # 1,500 km of 10,000 = 15%
    assert truck(api, 4)["services"]["oil_change"]["status"] == "ok"
    api.post("/api/fleet-health/maintenance-records", json=record(odometer_km=1400, performed_on=(today() - timedelta(days=5)).isoformat()))   # newest record wins: 8,100 km = 81%
    t = truck(api, 4)
    assert t["services"]["oil_change"]["status"] == "due_soon" and t["services"]["oil_change"]["usage_pct"] == 81.0
    api.post("/api/fleet-health/maintenance-records", json=record(odometer_km=-0 + 0, performed_on=(today() - timedelta(days=1)).isoformat()))      # 9,500 km = 95%
    assert truck(api, 4)["services"]["oil_change"]["status"] == "due_soon"
    api.post("/api/fleet-health/maintenance-records", json=record(service_type="tires", odometer_km=0, performed_on=today().isoformat()))             # 9,500 of 40,000 km
    assert truck(api, 4)["services"]["tires"]["status"] == "ok"


def test_overdue_service_puts_the_truck_in_needs_attention_with_a_breakdown(engine, db):
    db.add(daily_row(4, 0, odometer_end_km=30000.0))
    db.commit()
    api = client(engine)
    api.post("/api/fleet-health/maintenance-records", json=record(odometer_km=1000))          # 29,000 km on a 10,000 km interval
    summary = api.get("/api/fleet-health/summary").json()
    assert summary["kpis"]["services_overdue"] >= 1 and summary["kpis"]["trucks_needing_attention"] == 1
    attention = summary["needs_attention"][0]
    assert attention["plate"] == "NFX5791" and "Oil" in attention["overdue"]
    t = truck(api, 4)
    assert t["risk"]["breakdown"][0]["points"] == 40 and t["risk"]["needs_attention"] is True


def test_open_flags_and_failed_checklists_feed_the_risk_score(engine, db):
    api = client(engine)
    vehicle_flags.report_issue(4, "manual", "critical", "Engine knock", db=db)
    vehicle_flags.report_issue(4, "manual", "warning", "Mirror loose", db=db)
    db.commit()
    api.post("/api/fleet-health/pretrip-checklists", json={"vehicle_id": 4, "items": {**ALL_OK, "tires": "issue"}})
    t = truck(api, 4)
    parts = {p["key"]: p["points"] for p in t["risk"]["breakdown"]}
    assert parts["flags"] == 15 + 5 and parts["checklists"] == 5              # checklist flags are visible but not double-counted
    assert t["open_flags_count"] == 3 and t["critical_flags_count"] == 1 and t["last_checklist"]["passed"] is False


def test_intervals_placeholder_confirmation_and_overrides(engine):
    api = client(engine)
    assert api.get("/api/fleet-health/trucks").json()["intervals_unconfirmed"] is True
    for service_type, km in (("oil_change", 12000), ("tires", 40000), ("brakes", 30000), ("reefer_service", None), ("general_pms", 20000), ("battery", None)):
        body = {"service_type": service_type, "interval_km": km, "interval_days": 180 if km is None else None}
        if service_type == "reefer_service":
            body = {"service_type": service_type, "interval_engine_hours": 500, "interval_days": 180}
        assert api.put("/api/fleet-health/service-intervals", json=body).status_code == 200
    assert api.get("/api/fleet-health/trucks").json()["intervals_unconfirmed"] is False       # every default was edited/confirmed
    override = api.put("/api/fleet-health/service-intervals", json={"vehicle_id": 4, "service_type": "oil_change", "interval_km": 5000}).json()
    assert override["vehicle_id"] == 4 and truck(api, 4)["services"]["oil_change"]["status"] == "no_record"
    assert api.delete(f"/api/fleet-health/service-intervals/{override['id']}").status_code == 204
    default_id = api.put("/api/fleet-health/service-intervals", json={"service_type": "oil_change", "interval_km": 12000}).json()["id"]
    assert api.delete(f"/api/fleet-health/service-intervals/{default_id}").status_code == 422        # fleet defaults can be edited, not deleted
    assert api.put("/api/fleet-health/service-intervals", json={"service_type": "oil_change"}).status_code == 422
    assert api.put("/api/fleet-health/service-intervals", json={"service_type": "wings", "interval_km": 5}).status_code == 422
    assert api.put("/api/fleet-health/service-intervals", json={"service_type": "oil_change", "interval_km": 0}).status_code == 422
    assert client(engine, "warehouse").put("/api/fleet-health/service-intervals", json={"service_type": "oil_change", "interval_km": 9}).status_code == 403
    unconfirmed = api.put("/api/fleet-health/service-intervals", json={"service_type": "battery", "interval_days": 700, "confirmed": False}).json()
    assert unconfirmed["confirmed"] is False and api.get("/api/fleet-health/trucks").json()["intervals_unconfirmed"] is True


def test_third_party_motorcycle_and_tracked_trucks_are_presented_correctly(engine, db):
    db.add(daily_row(3, 0))
    db.commit()
    api = client(engine)
    data = api.get("/api/fleet-health/trucks").json()
    by_id = {t["id"]: t for t in data["trucks"]}
    third = by_id[-91001]
    assert third["kind"] == "third_party" and third["tracker"]["label"] == "Third-party" and "services" not in third and "risk" not in third and "not maintained by RGF" in third["message"]
    moto = by_id[7]
    assert moto["kind"] == "no_tracker" and moto["tracker"]["label"] == "No tracker" and moto["odometer_km"] is None
    assert all(s["status"] != "ok" or s["usage_pct"] is not None for s in moto["services"].values())     # never zeros without data
    assert by_id[3]["kind"] == "tracked" and by_id[3]["tracker"]["status"] in ("stale", "live") and by_id[3]["odometer_km"] == 10000.0
    assert "reefer_service" in by_id[3]["services"] and "reefer_service" not in by_id[4]["services"]      # reefer service only for reefer trucks
    assert by_id[1]["capacity_unconfirmed"] and by_id[2]["capacity_unconfirmed"] and not by_id[3]["capacity_unconfirmed"]
    kpis = api.get("/api/fleet-health/summary").json()["kpis"]
    assert kpis["maintained_trucks"] == 7 and -91001 not in by_id.keys() - {-91001}      # third-party excluded from fleet KPIs


def test_live_tracker_status_comes_from_the_sampler(engine, db):
    sampler.record(sampler.parse_status({"registration": "NFX5791", "event_ts": "2026-10-08 12:00:00+08", "odometer": 123456000, "ignition": True, "speed": 10, "vext": "13.9", "fuel": {"precentage_left": 80}}))
    t = truck(client(engine), 4)
    assert t["tracker"] == {**t["tracker"], "kind": "tracked", "status": "live", "label": "Live"} and t["odometer_km"] == 123456.0


def test_locked_for_repair_truck_offers_a_repair_record_until_one_is_open(engine, db):
    api = client(engine)
    t = truck(api, 5)                                   # NAJ6018 is locked "For Repair" in the fleet module
    assert t["locked_reason"] == "For Repair" and t["offer_repair_record"] is True
    api.post("/api/fleet-health/maintenance-records", json={"vehicle_id": 5, "kind": "repair", "performed_on": today().isoformat(), "downtime_start": datetime.now(MANILA).isoformat(), "reason": "Engine"})
    t = truck(api, 5)
    assert t["offer_repair_record"] is False and t["in_repair_record"] is True
    assert t["risk"]["breakdown"][4]["points"] == 5


def test_truck_detail_has_history_series_and_third_party_shape(engine, db):
    for offset in range(5):
        db.add(daily_row(4, offset, vext_parked_min=12.6 - offset * 0.2, vext_running_avg=14.0, electrical_system=12))
    db.commit()
    api = client(engine)
    api.post("/api/fleet-health/maintenance-records", json=record())
    detail = api.get("/api/fleet-health/trucks/4").json()
    assert detail["truck"]["plate"] == "NFX5791" and len(detail["records"]) == 1 and len(detail["daily"]) == 5 and detail["has_tracker"] is True
    assert len(detail["battery_series"]) == 5 and detail["battery_series"][0]["status"] in ("ok", "warning", "critical")
    assert {i["service_type"] for i in detail["intervals"]} == {"oil_change", "tires", "brakes", "general_pms", "battery"}
    assert api.get("/api/fleet-health/trucks/-91001").json().keys() == {"truck"}
    assert api.get("/api/fleet-health/trucks/12345").status_code == 404


def test_summary_is_cached_and_edits_invalidate_it(engine, monkeypatch):
    calls = []
    original = snapshot.build_snapshot
    monkeypatch.setattr(snapshot, "build_snapshot", lambda db: calls.append(1) or original(db))
    api = client(engine)
    api.get("/api/fleet-health/summary")
    api.get("/api/fleet-health/trucks")
    api.get("/api/fleet-health/summary")
    assert len(calls) == 1                               # one rebuild serves summary + trucks for 5 minutes
    api.post("/api/fleet-health/maintenance-records", json=record())
    api.get("/api/fleet-health/summary")
    assert len(calls) == 2


def test_roles_on_every_read_endpoint(engine):
    for path in ("/api/fleet-health/summary", "/api/fleet-health/trucks", "/api/fleet-health/trucks/4", "/api/fleet-health/eco/drivers", "/api/fleet-health/eco/trucks", "/api/fleet-health/eco/fuel-checks", "/api/fleet-health/eco/co2", "/api/fleet-health/fuel-logs", "/api/fleet-health/eco/scorecard"):
        for role in ("admin", "dispatcher", "planner", "warehouse"):
            assert client(engine, role).get(path).status_code == 200, (path, role)
        assert client(engine, "driver").get(path).status_code == 403, path


# =============================================================================================================== automation
def battery_rows(db, vehicle_id, parked_by_offset):
    for offset, parked in parked_by_offset.items():
        db.add(daily_row(vehicle_id, offset, vext_parked_min=parked, vext_running_avg=14.0, electrical_system=12))
    db.flush()


def test_nightly_battery_flags_follow_the_two_day_and_critical_rules(db):
    battery_rows(db, 4, {1: 12.1, 0: 12.0})
    raised = automation.evaluate_vehicle(db, vehicle_id=4, plate="NFX5791", day=today())
    assert [r["flag"]["severity"] for r in raised] == ["warning"] and raised[0]["flag"]["source"] == "battery"
    battery_rows(db, 5, {0: 11.5})
    assert automation.evaluate_vehicle(db, vehicle_id=5, plate="NAJ6018", day=today())[0]["flag"]["severity"] == "critical"
    battery_rows(db, 6, {1: 12.1, 0: 12.6})
    assert automation.evaluate_vehicle(db, vehicle_id=6, plate="NAN9911", day=today()) == []
    again = automation.evaluate_vehicle(db, vehicle_id=4, plate="NFX5791", day=today())
    assert again[0]["created"] is False                    # the same open flag is not duplicated


def test_nightly_fuel_flag_is_worded_as_a_check_and_notes_unconfirmed_capacity(db):
    db.add(daily_row(1, 0, parked_drop_litres_est=24.0))
    db.flush()
    [raised] = automation.evaluate_vehicle(db, vehicle_id=1, plate="DCD8953", day=today(), capacity_unconfirmed=True)
    message = raised["flag"]["message"]
    assert raised["flag"]["source"] == "fuel" and "check" in message and "estimate" in message and "unconfirmed" in message
    assert "theft" not in message.lower() and "stolen" not in message.lower()


# =============================================================================================================== eco
def seed_week(db, staff=13, vehicle=4, km=120.0, weeks_ago=1, **kw):
    start = config.MANILA and (today() - timedelta(days=today().weekday()) - timedelta(days=7 * weeks_ago))
    for i in range(5):
        db.add(VehicleDailyStat(vehicle_id=vehicle, stat_date=start + timedelta(days=i), km_driven=km / 5, trip_count=4, engine_seconds=16000, idle_seconds_total=1200, idle_seconds_at_stop=600, idle_seconds_elsewhere=600,
                                speeding_events=1, speeding_seconds=60, max_speed_kmh=85, harsh_braking=0, harsh_acceleration=0, harsh_cornering=0, assigned=True, primary_staff_id=staff, data_quality={"fuel_capacity_l": 100.0}, **kw))
    db.commit()


def test_eco_drivers_endpoint_scores_ranks_and_explains(engine, db):
    seed_week(db, staff=13, vehicle=4, km=200)
    seed_week(db, staff=7, vehicle=5, km=30)                          # under 50 km
    db.add(VehicleDailyStat(vehicle_id=3, stat_date=today() - timedelta(days=today().weekday() + 6), km_driven=500.0, engine_seconds=36000, assigned=False, data_quality={}))
    db.commit()
    data = client(engine).get("/api/fleet-health/eco/drivers").json()
    by_name = {d["name"]: d for d in data["drivers"]}
    assert by_name["Juan Dela Cruz"]["status"] == "scored" and by_name["Juan Dela Cruz"]["rank"] == 1 and len(by_name["Juan Dela Cruz"]["breakdown"]) == 4
    assert by_name["Pedro Reyes"]["status"] == "not_enough_data" and by_name["Pedro Reyes"]["score"] is None and by_name["Pedro Reyes"]["rank"] is None
    assert data["summary"]["scored"] == 1 and data["unassigned"]["km"] == 500.0 and data["min_km"] == 50.0
    assert data["week_start"] < today().isoformat()


def test_eco_trucks_endpoint_separates_unassigned_km_and_never_shows_zeros_for_missing_data(engine, db):
    seed_week(db, staff=13, vehicle=4, km=200)
    db.add(VehicleDailyStat(vehicle_id=4, stat_date=today() - timedelta(days=1), km_driven=50.0, engine_seconds=3600, idle_seconds_total=600, assigned=False, data_quality={}))
    db.add(VehicleDailyStat(vehicle_id=4, stat_date=today() - timedelta(days=2), km_driven=None, engine_seconds=3600, idle_seconds_total=300, assigned=True, primary_staff_id=13, data_quality={"km_source": "none"}))
    db.commit()
    data = client(engine).get("/api/fleet-health/eco/trucks").json()
    by_plate = {t["plate"]: t for t in data["trucks"]}
    assert by_plate["NFX5791"]["totals"]["unassigned_km"] == 50.0 and by_plate["NFX5791"]["totals"]["assigned_km"] == 200.0
    assert by_plate["NFX5791"]["totals"]["days_missing_km"] == 1
    assert by_plate["NFX5791"]["totals"]["unclassified_idle_min"] == 10.0 and by_plate["NFX5791"]["kmpl"] is None and by_plate["NFX5791"]["litres"] is None and by_plate["NFX5791"]["co2_kg"] is None
    assert by_plate["DCD8955"]["totals"] is None and by_plate["DCD8955"]["days_with_data"] == 0          # tracked but no data yet: null, not zero
    assert "Motorcycle 1" not in by_plate and "ASIAN CONNECT" not in by_plate


def test_fuel_checks_are_labelled_estimates_and_log_checks_appear(engine, db):
    db.add(daily_row(1, 1, parked_drop_litres_est=20.0, refuel_events=1, refuel_litres_est=80.0))
    db.add(FuelLog(vehicle_id=3, filled_at=datetime.now(timezone.utc) - timedelta(days=1), litres=400, amount_php=24000, full_tank=False))
    db.add(daily_row(3, 1))
    db.commit()
    data = client(engine).get("/api/fleet-health/eco/fuel-checks").json()
    kinds = {c["kind"] for c in data["sensor_checks"]}
    assert kinds == {"parked_drop", "refuel"} and all("estimate" in c["text"] for c in data["sensor_checks"])
    assert any("unconfirmed" in c["text"] for c in data["sensor_checks"])                               # DCD8953 capacity
    assert any("more than the 100 L tank" in c["reason"] for c in data["log_checks"]) and "estimate" in data["label"].lower()


def test_co2_by_month_uses_2_68_kg_per_litre(engine, db):
    now = datetime.now(timezone.utc)
    db.add_all([FuelLog(vehicle_id=4, filled_at=now - timedelta(days=1), litres=100, amount_php=6000), FuelLog(vehicle_id=3, filled_at=now - timedelta(days=1), litres=50, amount_php=3000)])
    db.commit()
    data = client(engine).get("/api/fleet-health/eco/co2").json()
    month = data["months"][-1]
    assert month["litres"] == 150.0 and month["co2_kg"] == 402.0 and month["trucks"]["NFX5791"]["co2_kg"] == 268.0 and data["kg_per_litre"] == 2.68
    kpis = client(engine).get("/api/fleet-health/summary").json()["kpis"]
    assert kpis["co2_month_kg"] in (None, 402.0)                  # null only when the month rolled over between the two requests


def test_summary_kpis_without_any_data_are_null_not_zero(engine):
    kpis = client(engine).get("/api/fleet-health/summary").json()["kpis"]
    assert kpis["fleet_kmpl_30d"] is None and kpis["co2_month_kg"] is None and kpis["avg_eco_score_last_week"] is None and kpis["open_issues"] == 0


# =============================================================================================================== weekly WhatsApp scorecard
def test_scorecard_default_off_admin_only_and_preview_sends_nothing(engine, db, monkeypatch):
    seed_week(db, staff=13, vehicle=4, km=200)
    seed_week(db, staff=7, vehicle=5, km=30)
    monkeypatch.setattr(scorecard.staff_directory_cache, "get_by_id", lambda sid, **kw: {13: {"phone": "0917 111 2222"}, 7: {"phone": None}}.get(sid))
    sent = []
    monkeypatch.setattr("routers.dispatch.send_message", lambda body: sent.append(body))
    api = client(engine, "dispatcher")
    data = api.get("/api/fleet-health/eco/scorecard").json()
    assert data["switch"]["enabled"] is False and data["switch"]["default"] is False
    items = {i["name"]: i for i in data["preview"]["items"]}
    assert items["Juan Dela Cruz"]["will_send"] is True and "Hi Juan!" in items["Juan Dela Cruz"]["message"]
    assert items["Pedro Reyes"]["will_send"] is False and items["Pedro Reyes"]["skip_reason"] == "Not enough data this week"
    assert sent == []
    assert api.put("/api/fleet-health/eco/scorecard", json={"enabled": True}).status_code == 403
    assert client(engine, "admin").put("/api/fleet-health/eco/scorecard", json={"enabled": True}).json()["enabled"] is True


def test_weekly_job_respects_the_switch_whatsapp_pause_and_missing_phones(engine, db, monkeypatch):
    seed_week(db, staff=13, vehicle=4, km=200)
    seed_week(db, staff=21, vehicle=5, km=200)
    monkeypatch.setattr(scorecard.staff_directory_cache, "get_by_id", lambda sid, **kw: {13: {"phone": "0917 111 2222"}, 21: {"phone": ""}}.get(sid))
    sent = []

    async def fake_send(body):
        sent.append(body)
        return {"messages": []}

    monkeypatch.setattr("routers.dispatch.send_message", fake_send)
    run = lambda: asyncio.run(scorecard.send_weekly(db))
    assert run()["reason"] == "scorecard switch is off" and sent == []
    scorecard.set_enabled(True, "Pau")
    assert run()["reason"] == "WhatsApp is paused" and sent == []                      # WhatsApp itself is paused by default
    whatsapp_control.set_mode("active", actor_name="Pau")
    result = run()
    assert result["sent"] == 1 and result["skipped"] == 1                              # staff 21 has no phone number
    assert sent[0].recipient_id == 13 and sent[0].channels == ["whatsapp"] and sent[0].trigger_event == "eco_scorecard"


# =============================================================================================================== email hook, safety
def test_email_agent_issue_creates_a_flag_on_the_assigned_truck(monkeypatch):
    from services import logistics_email_agent as agent

    captured = []
    monkeypatch.setattr(vehicle_flags, "report_issue", lambda *a, **k: captured.append((a, k)))
    context = {"truckPlate": "NFX5791, DCD8955"}
    agent._record_vehicle_issue(staff={"name": "Juan"}, context=context, action="ESCALATE", driver_message="Flat tire  on the highway", message_id="m1")
    assert [(c[0][0], c[0][1], c[0][2]) for c in captured] == [("NFX5791", "email", "critical"), ("DCD8955", "email", "critical")]
    assert "Flat tire on the highway" in captured[0][0][3] and captured[0][1]["ref"] == "email:m1"
    captured.clear()
    agent._record_vehicle_issue(staff={"name": "Juan"}, context={"truckPlate": None}, action="ACK_ISSUE", driver_message="x", message_id="m2")
    assert captured == []
    agent._record_vehicle_issue(staff={"name": "Juan"}, context=context, action="ACK_ISSUE", driver_message="Leak", message_id="m3")
    assert captured[0][0][2] == "warning"


def test_email_hook_never_raises(monkeypatch):
    from services import logistics_email_agent as agent

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(vehicle_flags, "report_issue", boom)
    agent._record_vehicle_issue(staff={"name": "Juan"}, context={"truckPlate": "NFX5791"}, action="ACK_ISSUE", driver_message="x", message_id="m")


def test_tests_cannot_reach_the_real_database():
    with pytest.raises(RuntimeError, match="real database is never opened"):
        vehicle_flags.report_issue(4, "manual", "info", "no db passed")


# =============================================================================================================== scheduler wiring
def test_jobs_register_sampler_nightly_and_monday_scorecard():
    from services.fleet_health import jobs

    added = []

    class FakeScheduler:
        def add_job(self, func, **kw):
            added.append((func.__name__, kw))

    jobs.register(FakeScheduler())
    by_id = {kw["id"]: kw for _, kw in added}
    assert set(by_id) == {"fleet_health_sampler", "fleet_health_nightly", "fleet_health_scorecard"}
    assert by_id["fleet_health_sampler"]["seconds"] == 600 and by_id["fleet_health_sampler"]["max_instances"] == 1
    nightly = by_id["fleet_health_nightly"]
    assert (nightly["hour"], nightly["minute"], str(nightly["timezone"])) == (1, 0, "Asia/Manila")
    card = by_id["fleet_health_scorecard"]
    assert (card["day_of_week"], card["hour"], str(card["timezone"])) == ("mon", 8, "Asia/Manila")
