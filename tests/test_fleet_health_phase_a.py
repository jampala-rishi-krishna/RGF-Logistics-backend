"""Fleet Health phase A: matching, shared Cartrack limiter + retries, daily-stat calculations, sampler, nightly/backfill,
migration shape, roles. Cartrack, Google and the database are all faked."""
import asyncio
import io
import itertools
import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("JWT_SECRET", "test")

from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from services import cartrack_client, cartrack_limiter
from services.fleet_health import config, daily, sampler, service
from services.fleet_health.matching import classify, normalize_plate

MANILA = config.MANILA
DAY = date(2026, 10, 6)


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(type_, compiler, **kw):  # lets SQLite autoincrement BIGINT primary keys in these tests
    return "INTEGER"


_trip_ids = itertools.count(1)
ASSIGNED = [{"driver_id": 13}]


def trip(start="2026-10-06 08:00:00+08", **overrides):
    base = {"trip_id": next(_trip_ids), "start_timestamp": start, "end_timestamp": "2026-10-06 09:00:00+08", "trip_duration_seconds": 3600,
            "start_odometer": 1_000_000, "end_odometer": 1_010_000, "trip_distance": 10_000, "max_speed": 60, "idle_time_seconds": 0,
            "harsh_braking_events": 0, "harsh_acceleration_events": 0, "harsh_cornering_events": 0, "road_speeding_events": 0,
            "road_speeding_duration_seconds": None, "start_coordinates": {"latitude": 14.5, "longitude": 121.0}, "end_coordinates": {"latitude": 14.6, "longitude": 121.1}}
    return {**base, **overrides}


VEHICLE = {"id": 3, "plate": "DCD8955", "plate_key": "DCD8955", "fuel_capacity_l": 100.0}


def sample(hhmm, *, ignition=False, speed=0, fuel=None, vext=None, day=DAY):
    h, m = map(int, hhmm.split(":"))
    return {"ts": datetime(day.year, day.month, day.day, h, m, tzinfo=MANILA), "ignition": ignition, "speed": speed, "fuel_pct": fuel, "vext": vext, "odometer_m": None}


def row_for(trips=(), samples=None, sites=(), assignments=None, vehicle=VEHICLE):
    return daily.compute_daily_row(vehicle=vehicle, day=DAY, trips=list(trips), samples=samples, sites=list(sites), assignments=assignments)


# ---- matching ----------------------------------------------------------------------------------

def test_matching_classifies_tracked_third_party_and_no_tracker():
    vehicles = [{"id": 1, "plate_no": "dcd-8953", "is_third_party": False}, {"id": 2, "plate_no": "ASIAN CONNECT", "is_third_party": True}, {"id": 3, "plate_no": "Motorcycle 1", "is_third_party": False}]
    roster = [{"registration": "DCD8953", "vehicle_id": 11, "fuel_capacity": 200}, {"registration": "ZZZ999", "vehicle_id": 12, "fuel_capacity": 50}]
    result = classify(vehicles, roster)
    kinds = {v["plate"]: v["kind"] for v in result["fleet"]}
    assert kinds == {"dcd-8953": "tracked", "ASIAN CONNECT": "third_party", "Motorcycle 1": "no_tracker"}
    assert result["tracked"][0]["fuel_capacity_l"] == 200.0 and result["tracked"][0]["cartrack_vehicle_id"] == 11
    assert result["cartrack_not_in_rarechain"] == ["ZZZ999"]
    assert normalize_plate(" nan-9911 ") == "NAN9911"


# ---- shared limiter + retries --------------------------------------------------------------------

def test_limiter_allows_20_per_minute_then_waits():
    clock = {"t": 0.0}
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    limiter = cartrack_limiter.SharedRateLimiter(max_calls=20, clock=lambda: clock["t"], sleep=fake_sleep)

    async def go():
        for _ in range(25):
            await limiter.acquire()

    asyncio.run(go())
    assert limiter.max_calls == 20
    assert slept and abs(sum(slept) - 60.0) < 0.5      # the 21st call waits for the first to leave the 60 s window


def test_poller_calls_count_against_the_shared_budget_without_waiting():
    limiter = cartrack_limiter.SharedRateLimiter(max_calls=3, clock=lambda: 0.0, sleep=lambda s: asyncio.sleep(0))
    for _ in range(3):
        limiter.note()
    assert limiter.in_window() == 3


class FakeResponse:
    def __init__(self, status, payload=None, headers=None, bad_json=False):
        self.status_code, self._payload, self.headers, self._bad = status, payload, headers or {}, bad_json
        self.text = "x"

    def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._payload


class FakeClient:
    def __init__(self, script):
        self.script = script

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run_get(script, path="/rest/trips/X"):
    sleeps, counter = [], cartrack_limiter.CartrackCallCounter()

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    limiter = cartrack_limiter.SharedRateLimiter(max_calls=1000, sleep=fake_sleep)
    os.environ.setdefault("CARTRACK_USERNAME", "u")
    os.environ.setdefault("CARTRACK_API_KEY", "k")
    result = asyncio.run(cartrack_client.get_json(path, counter=counter, limiter=limiter, sleep=fake_sleep, client_factory=lambda: FakeClient(script)))
    return result, sleeps, counter


def test_retries_after_1_2_4_8_seconds_on_429_and_5xx():
    script = [FakeResponse(429), FakeResponse(500), FakeResponse(503), FakeResponse(502), FakeResponse(200, {"data": []})]
    result, sleeps, counter = run_get(script)
    assert result == {"data": []} and sleeps == [1, 2, 4, 8] and counter.calls == 5


def test_timeouts_and_invalid_json_are_retried_then_give_up():
    result, sleeps, _ = run_get([httpx.ReadTimeout("t"), FakeResponse(200, bad_json=True), FakeResponse(200, {"ok": 1})])
    assert result == {"ok": 1} and sleeps == [1, 2]
    with pytest.raises(cartrack_client.CartrackError):
        run_get([FakeResponse(500)] * 5)


def test_non_retryable_status_fails_immediately():
    with pytest.raises(cartrack_client.CartrackError) as caught:
        run_get([FakeResponse(404)])
    assert caught.value.status_code == 404


def test_retry_after_hint_is_honoured_when_larger():
    _, sleeps, _ = run_get([FakeResponse(429, headers={"X-RateLimit-Retry-After-Seconds": "30"}), FakeResponse(200, {})])
    assert sleeps == [30.0]


# ---- distance, engine hours ------------------------------------------------------------------------

def test_km_uses_the_day_odometer_first():
    r = row_for([trip(start_odometer=1_000_000, end_odometer=1_010_000), trip(start="2026-10-06 10:00:00+08", start_odometer=1_020_000, end_odometer=1_050_000, trip_distance=1)])
    assert (r["odometer_start_km"], r["odometer_end_km"], r["km_driven"]) == (1000.0, 1050.0, 50.0)
    assert r["data_quality"]["km_source"] == "odometer"


def test_km_falls_back_to_trip_distance_then_per_trip_odometer():
    r = row_for([trip(start_odometer=None, end_odometer=None, trip_distance=4_000), trip(start="2026-10-06 10:00:00+08", start_odometer=None, end_odometer=None, trip_distance=6_000)])
    assert r["km_driven"] == 10.0 and r["data_quality"]["km_source"] == "trip_distance"
    # no day odometer pair (the LAST trip has no end odometer) and no distances: sum the per-trip odometer differences
    r = row_for([trip(trip_distance=None, start_odometer=1_000, end_odometer=4_000), trip(start="2026-10-06 10:00:00+08", trip_distance=None, start_odometer=5_000, end_odometer=None)])
    assert r["odometer_end_km"] == 4.0
    r = row_for([trip(trip_distance=None, start_odometer=None, end_odometer=None)])
    assert r["km_driven"] is None and r["data_quality"]["km_source"] == "none"
    assert r["data_quality"]["trips_missing_distance"] == 1


def test_a_tracked_day_without_trips_is_zero_km_not_missing():
    r = row_for([])
    assert (r["trip_count"], r["km_driven"], r["engine_seconds"]) == (0, 0.0, 0)


def test_engine_seconds_trip_count_speeding_harsh_and_max_speed():
    r = row_for([trip(trip_duration_seconds=3600, max_speed=70, road_speeding_events=3, road_speeding_duration_seconds=120, harsh_braking_events=1),
                 trip(start="2026-10-06 12:00:00+08", trip_duration_seconds=1800, max_speed=95, harsh_cornering_events=2, harsh_acceleration_events=4)])
    assert r["engine_seconds"] == 5400 and r["trip_count"] == 2           # engine hours = 5400 / 3600 = 1.5
    assert (r["speeding_events"], r["speeding_seconds"], r["max_speed_kmh"]) == (3, 120, 95)
    assert (r["harsh_braking"], r["harsh_acceleration"], r["harsh_cornering"]) == (1, 4, 2)


def test_a_trip_belongs_to_the_manila_day_it_starts():
    late = trip(start="2026-10-05 23:30:00+08")          # starts 5 Oct Manila (ends after midnight): NOT the 6th
    early = trip(start="2026-10-06 00:10:00+08")
    utc_late = trip(start="2026-10-05 17:00:00+00")      # 01:00 on the 6th in Manila
    assert [t["start_timestamp"] for t in daily.trips_of_day([late, early, utc_late], DAY)] == ["2026-10-06 00:10:00+08", "2026-10-05 17:00:00+00"]


# ---- idle split ------------------------------------------------------------------------------------

MET, GLACIER = config.WAREHOUSE_SITES["Mets"], config.WAREHOUSE_SITES["Glacier"]


def near(site, metres_north=0.0):
    return {"latitude": site[0] + metres_north / 111_320.0, "longitude": site[1]}


def test_idle_at_a_warehouse_vs_elsewhere():
    at_mets = trip(idle_time_seconds=600, start_coordinates=near(MET, 150), end_coordinates={"latitude": 14.0, "longitude": 121.5})
    at_glacier_end = trip(start="2026-10-06 10:00:00+08", idle_time_seconds=300, start_coordinates={"latitude": 14.0, "longitude": 121.5}, end_coordinates=near(GLACIER, 50))
    elsewhere = trip(start="2026-10-06 12:00:00+08", idle_time_seconds=900, start_coordinates=near(MET, 500), end_coordinates={"latitude": 14.0, "longitude": 121.5})
    r = row_for([at_mets, at_glacier_end, elsewhere], sites=config.WAREHOUSE_SITES.values(), assignments=ASSIGNED)
    assert (r["idle_seconds_total"], r["idle_seconds_at_stop"], r["idle_seconds_elsewhere"]) == (1800, 900, 900)
    assert r["data_quality"]["idle_split"] == "classified"


def test_idle_at_an_assigned_delivery_point_counts_as_at_stop():
    delivery = (14.6500, 121.0500)
    t = trip(idle_time_seconds=1200, start_coordinates={"latitude": 14.3, "longitude": 121.0}, end_coordinates={"latitude": 14.6501, "longitude": 121.0501})
    assert row_for([t], sites=[delivery], assignments=ASSIGNED)["idle_seconds_at_stop"] == 1200
    assert row_for([t], sites=[], assignments=ASSIGNED)["idle_seconds_elsewhere"] == 1200      # without that SO point, the same idle is "elsewhere"


def test_idle_is_unclassified_on_days_without_an_assignment():
    t = trip(idle_time_seconds=1200, start_coordinates=near(MET, 50), end_coordinates=near(MET, 60))
    r = row_for([t], sites=config.WAREHOUSE_SITES.values(), assignments=[])
    assert r["idle_seconds_total"] == 1200 and r["idle_seconds_at_stop"] is None and r["idle_seconds_elsewhere"] is None
    assert r["data_quality"]["idle_split"] == "unclassified" and r["data_quality"]["idle_split_note"] == "no assignment data, idle not classified"


# ---- trip data quality: duplicates, overlaps, engine-time caps ---------------------------------------------

def test_repeated_trip_ids_are_dropped_once():
    t = trip()
    r = row_for([t, dict(t), trip(start="2026-10-06 10:00:00+08")])
    assert r["trip_count"] == 2 and r["engine_seconds"] == 7200 and r["data_quality"]["duplicate_trips_dropped"] == 1


def test_overlapping_trips_count_their_shared_time_once():
    a = trip(start="2026-10-06 08:00:00+08", trip_duration_seconds=3600)
    b = trip(start="2026-10-06 08:30:00+08", trip_duration_seconds=3600)      # 30 min inside a
    r = row_for([a, b])
    assert r["engine_seconds"] == 5400 and r["data_quality"]["overlapping_trip_seconds"] == 1800
    assert r["engine_seconds"] <= 86400


def test_engine_seconds_never_exceed_a_day_or_the_trip_span():
    huge = trip(start="2026-10-06 00:00:00+08", trip_duration_seconds=100_000)
    r = row_for([huge])
    assert r["engine_seconds"] == 86400 and r["data_quality"]["engine_seconds_capped"] == {"raw_sum": 100000, "capped_to": 86400}
    clean = row_for([trip(trip_duration_seconds=3600)])
    assert clean["engine_seconds"] == 3600 and "engine_seconds_capped" not in clean["data_quality"]


def test_fetch_trips_drops_a_row_repeated_on_two_pages(monkeypatch):
    async def fake_get_json(path, params=None, *, counter=None, **kw):
        page = params["page"]
        rows = [{"trip_id": 1}, {"trip_id": 2}] if page == 1 else [{"trip_id": 2}, {"trip_id": 3}]   # id 2 comes back twice
        return {"data": rows, "meta": {"last_page": 2}}

    monkeypatch.setattr(cartrack_client, "get_json", fake_get_json)
    counter = cartrack_limiter.CartrackCallCounter()
    trips = asyncio.run(service.fetch_trips("X", DAY, DAY, counter=counter))
    assert [t["trip_id"] for t in trips] == [1, 2, 3] and counter.trip_duplicates == 1


# ---- battery ---------------------------------------------------------------------------------------

def test_battery_12v_and_24v_detection_and_parked_minimum():
    twelve = row_for(samples=[sample("06:00", vext=12.7), sample("07:00", ignition=True, speed=30, vext=14.1), sample("08:00", ignition=True, speed=30, vext=14.3), sample("22:00", vext=12.4)])
    assert (twelve["electrical_system"], twelve["vext_parked_min"], twelve["vext_running_avg"]) == (12, 12.4, 14.2)
    twenty_four = row_for(samples=[sample("06:00", vext=25.1), sample("07:00", ignition=True, speed=30, vext=27.9), sample("22:00", vext=24.6)])
    assert (twenty_four["electrical_system"], twenty_four["vext_parked_min"], twenty_four["vext_running_avg"]) == (24, 24.6, 27.9)


# ---- fuel ------------------------------------------------------------------------------------------

def test_refuel_is_a_rise_over_10_points_while_stationary():
    r = row_for(samples=[sample("08:00", fuel=30), sample("08:10", fuel=80), sample("08:20", fuel=80)])
    assert (r["refuel_events"], r["refuel_litres_est"]) == (1, 50.0)       # 50 points x 100 L
    assert (r["fuel_pct_start"], r["fuel_pct_end"]) == (30.0, 80.0)
    moving = row_for(samples=[sample("08:00", fuel=30, speed=40, ignition=True), sample("08:10", fuel=80, speed=40, ignition=True)])
    assert moving["refuel_events"] == 0                                    # a rise while moving is sensor slosh, not a refuel
    assert row_for(samples=[sample("08:00", fuel=50), sample("08:10", fuel=58)])["refuel_events"] == 0   # 8 points < 10


def test_parked_drop_needs_ignition_off_over_5_points_within_2_hours():
    drop = row_for(samples=[sample("01:00", fuel=60), sample("01:10", fuel=58), sample("01:20", fuel=50)])
    assert drop["parked_drop_litres_est"] == 10.0                           # 10 points x 100 L (a "check", never theft)
    on = row_for(samples=[sample("01:00", fuel=60, ignition=True), sample("01:10", fuel=50, ignition=True)])
    assert on["parked_drop_litres_est"] == 0.0
    slow = row_for(samples=[sample("01:00", fuel=60), sample("03:30", fuel=50)])
    assert slow["parked_drop_litres_est"] == 0.0                            # 2.5 h apart: outside the 2 h window


def test_missing_fuel_capacity_gives_no_litres_and_unconfirmed_capacity_is_marked():
    r = row_for(samples=[sample("08:00", fuel=30), sample("08:10", fuel=80)], vehicle={**VEHICLE, "fuel_capacity_l": None})
    assert r["refuel_events"] == 1 and r["refuel_litres_est"] is None and r["data_quality"]["fuel_capacity_missing"] is True
    unconfirmed = row_for(samples=[sample("08:00", fuel=30)], vehicle={**VEHICLE, "plate_key": "DCD8953", "fuel_capacity_l": 200.0})
    assert unconfirmed["data_quality"]["capacity_unconfirmed"] is True and "capacity_unconfirmed" not in row_for(samples=[sample("08:00", fuel=30)])["data_quality"]


# ---- data quality / backfill rows ---------------------------------------------------------------------

def test_backfilled_days_have_null_fuel_and_battery_and_say_why():
    r = row_for([trip()], samples=None)
    for key in ("vext_parked_min", "vext_running_avg", "electrical_system", "fuel_pct_start", "fuel_pct_end", "refuel_events", "refuel_litres_est", "parked_drop_litres_est"):
        assert r[key] is None, key
    assert r["data_quality"]["no_status_samples"] is True and r["data_quality"]["partial_day"] is False


def test_partial_day_when_the_sampler_has_a_long_gap_or_no_samples():
    full = [sample(f"{h:02d}:{m:02d}", fuel=50, vext=12.5) for h in range(24) for m in range(0, 60, 10)]
    assert row_for(samples=full)["data_quality"]["partial_day"] is False
    gappy = [s for s in full if not (6 <= s["ts"].hour < 8)]
    assert row_for(samples=gappy)["data_quality"]["partial_day"] is True
    assert row_for(samples=[])["data_quality"]["partial_day"] is True


# ---- driver attribution --------------------------------------------------------------------------------

def test_primary_driver_is_only_set_when_attribution_is_unambiguous():
    r = row_for([trip()], assignments=[{"driver_id": 13}, {"driver_id": 13}, {"driver_id": 7}])
    assert (r["primary_staff_id"], r["assigned"]) == (None, True)
    assert r["data_quality"]["ambiguous_driver_attribution"] is True
    r = row_for([trip()], assignments=[{"driver_id": 13}, {"driver_id": 13}])
    assert (r["primary_staff_id"], r["assigned"]) == (13, True)
    r = row_for([trip()], assignments=[])
    assert (r["primary_staff_id"], r["assigned"]) == (None, False)


# ---- sampler -------------------------------------------------------------------------------------------

STATUS = {"registration": "NFX 5791", "event_ts": "2026-10-06 03:00:00+08", "odometer": 193350718, "ignition": False, "idling": False, "speed": 0, "vext": "12.66", "fuel": {"level": 52, "precentage_left": 94}, "rpm": 0, "temp1": None}


def test_sample_keeps_only_verified_fields_and_uses_capture_time():
    captured = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)
    s = sampler.parse_status(STATUS, captured)
    assert s["plate_key"] == "NFX5791" and s["fuel_pct"] == 94.0 and s["vext"] == 12.66 and s["odometer_m"] == 193350718.0
    assert s["ts"] == captured.astimezone(MANILA) and s["event_ts"].day == 6
    assert not ({"rpm", "temp1", "clock", "driver"} & set(s))


def test_parked_truck_repeating_one_event_still_counts_as_covered():
    sampler.reset()
    base = datetime(2026, 10, 6, 0, 0, tzinfo=MANILA)
    for i in range(144):
        sampler.record(sampler.parse_status(STATUS, base + timedelta(minutes=10 * i)))
    stored = sampler.samples_for("NFX5791")
    assert len(stored) == 144 and daily.coverage(stored, DAY)["partial_day"] is False
    sampler.reset()


def test_sample_once_makes_one_call_and_never_raises(monkeypatch):
    sampler.reset()
    counter = cartrack_limiter.CartrackCallCounter()

    async def fake_get_json(path, params=None, *, counter=None, **kw):
        counter.add(path)
        return {"data": [STATUS, {**STATUS, "registration": "DCD8955"}]}

    monkeypatch.setattr(cartrack_client, "get_json", fake_get_json)
    assert asyncio.run(sampler.sample_once(counter)) == 2 and counter.calls == 1

    async def boom(*a, **k):
        raise cartrack_client.CartrackError("down")

    monkeypatch.setattr(cartrack_client, "get_json", boom)
    assert asyncio.run(sampler.sample_once()) == 0 and sampler.last_run()["ok"] is False
    sampler.reset()


# ---- trips paging ---------------------------------------------------------------------------------------

def test_fetch_trips_pages_with_limit_until_last_page(monkeypatch):
    seen = []

    async def fake_get_json(path, params=None, *, counter=None, **kw):
        seen.append((path, params["limit"], params["page"]))
        page = params["page"]
        return {"data": [{"trip_id": page * 10 + i} for i in range(2)], "meta": {"last_page": 3}}

    monkeypatch.setattr(cartrack_client, "get_json", fake_get_json)
    trips = asyncio.run(service.fetch_trips("NFX5791", DAY, DAY))
    assert len(trips) == 6 and seen == [("/rest/trips/NFX5791", 200, 1), ("/rest/trips/NFX5791", 200, 2), ("/rest/trips/NFX5791", 200, 3)]


# ---- nightly + backfill against SQLite ---------------------------------------------------------------------

@pytest.fixture()
def db():
    from database import Base
    import models  # noqa: F401  (registers every table)
    from models.fleet_health import VehicleDailyStat
    from models.vehicle import Vehicle

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Vehicle.__table__, VehicleDailyStat.__table__])
    with sessionmaker(bind=engine)() as session:
        session.add(Vehicle(id=3, plate_no="DCD8955", is_gps_tracked=True, is_third_party=False))
        session.commit()
        yield session


def fake_fleet(monkeypatch, trips_by_plate):
    entry = {"vehicle_id": 3, "plate": "DCD8955", "plate_key": "DCD8955", "kind": "tracked", "cartrack_vehicle_id": 11, "registration": "DCD8955", "fuel_capacity_l": 100.0}

    async def fake_match(db, *, counter=None):
        counter.add("/rest/vehicles")
        return {"tracked": [entry], "fleet": [entry], "third_party": [], "no_tracker": [], "cartrack_not_in_rarechain": []}

    async def fake_trips(registration, first, last, *, counter=None, limit=None):
        counter.add(f"/rest/trips/{registration}")
        return list(trips_by_plate)

    monkeypatch.setattr(service, "match_fleet", fake_match)
    monkeypatch.setattr(service, "fetch_trips", fake_trips)
    monkeypatch.setattr(service, "delivery_points", lambda assignments, geocode=True: asyncio.sleep(0, ([], 0)))
    monkeypatch.setattr(service, "load_assignments", lambda db, plates, first, last: {})


def test_nightly_is_idempotent_one_row_per_vehicle_day(monkeypatch, db):
    from models.fleet_health import VehicleDailyStat

    fake_fleet(monkeypatch, [trip()])
    first = asyncio.run(service.run_nightly(db, DAY))
    second = asyncio.run(service.run_nightly(db, DAY))
    rows = db.execute(select(VehicleDailyStat)).scalars().all()
    assert len(rows) == 1 and rows[0].stat_date == DAY and float(rows[0].km_driven) == 10.0
    assert first["written"] == {"DCD8955": "inserted"} and second["written"] == {"DCD8955": "updated"}
    assert first["cartrack_calls"] == 2        # 1 roster + 1 trips page for one truck


def test_nightly_dry_run_writes_nothing(monkeypatch, db):
    from models.fleet_health import VehicleDailyStat

    fake_fleet(monkeypatch, [trip()])
    result = asyncio.run(service.run_nightly(db, DAY, dry_run=True))
    assert result["rows"][0]["km_driven"] == 10.0 and result["written"] == {}
    assert db.execute(select(VehicleDailyStat)).scalars().all() == []


def test_a_second_job_does_not_run_while_one_is_running(monkeypatch, db):
    fake_fleet(monkeypatch, [trip()])

    async def go():
        async with service._run_lock:
            return await service.run_nightly(db, DAY)

    assert "skipped" in asyncio.run(go())


def test_backfill_dry_run_reports_rows_and_calls_and_defaults_to_no_write(monkeypatch, db):
    from models.fleet_health import VehicleDailyStat

    yesterday = datetime.now(MANILA).date() - timedelta(days=1)
    fake_fleet(monkeypatch, [trip(start=f"{yesterday} 08:00:00+08")])
    result = asyncio.run(service.run_backfill(db, 3))
    assert result["dry_run"] is True and result["rows_total"] == 3 and result["written"] == 0 and result["cartrack_calls"] == 2
    assert all(r["data_quality"]["backfill"] and r["data_quality"]["no_status_samples"] for r in result["rows"])
    assert db.execute(select(VehicleDailyStat)).scalars().all() == []
    written = asyncio.run(service.run_backfill(db, 3, dry_run=False))
    assert written["written"] == 3 and len(db.execute(select(VehicleDailyStat)).scalars().all()) == 3
    asyncio.run(service.run_backfill(db, 3, dry_run=False))
    assert len(db.execute(select(VehicleDailyStat)).scalars().all()) == 3      # re-running overwrites, never duplicates


# ---- migration shape -----------------------------------------------------------------------------------------

def render(direction):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations", "versions", "j1b2c3d4e5f6_fleet_health.py")
    spec = importlib.util.spec_from_file_location("fleet_health_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        getattr(module, direction)()
    return module, buffer.getvalue()


def test_migration_creates_six_tables_with_an_open_only_dedup_index():
    module, sql = render("upgrade")
    assert module.down_revision == "i1a2b3c4d5e6" and sql.count("CREATE TABLE") == 6
    assert "uq_vehicle_flags_dedup" not in sql
    assert "CREATE UNIQUE INDEX ux_vehicle_flags_open_dedup ON vehicle_flags (dedup_key) WHERE resolved_at IS NULL" in sql
    assert "'driver_app'" in sql and "confirmed BOOLEAN DEFAULT false NOT NULL" in sql
    assert sql.count("INSERT INTO service_intervals") == 1 and "'oil_change',     10000, 250, 180, true, false" in sql
    assert "UNIQUE (vehicle_id, stat_date)" in sql
    _, down = render("downgrade")
    assert down.count("DROP TABLE") == 6


def test_alembic_has_a_single_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config_obj = Config(os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini"))
    config_obj.set_main_option("script_location", os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations"))
    assert ScriptDirectory.from_config(config_obj).get_heads() == ["m3e4f5a6b7c8"]


# ---- roles --------------------------------------------------------------------------------------------------

def client_as(role, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from auth.dependencies import CurrentUser, get_current_user
    from database import get_db
    from routers import fleet_health

    captured = {}

    async def fake_backfill(db, days, *, dry_run=True, **kw):
        captured.update(days=days, dry_run=dry_run)
        return {"dry_run": dry_run, "rows": [{"vehicle": "X"}] * 5, "cartrack_calls": 20}

    async def fake_match(db, *, counter=None):
        return {"fleet": [], "tracked": [], "third_party": [], "no_tracker": [], "cartrack_not_in_rarechain": []}

    monkeypatch.setattr(fleet_health.service, "run_backfill", fake_backfill)
    monkeypatch.setattr(fleet_health, "match_fleet", fake_match)
    app = FastAPI()
    app.include_router(fleet_health.router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=1, email="x@x.com", role=role, full_name="X", status="active")
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    return TestClient(app), captured


@pytest.mark.parametrize("role", ["dispatcher", "warehouse", "driver"])
def test_backfill_is_admin_only(role, monkeypatch):
    client, captured = client_as(role, monkeypatch)
    assert client.post("/api/fleet-health/backfill").status_code == 403 and captured == {}


def test_admin_backfill_defaults_to_dry_run(monkeypatch):
    client, captured = client_as("admin", monkeypatch)
    response = client.post("/api/fleet-health/backfill")
    assert response.status_code == 200 and captured == {"days": 30, "dry_run": True} and len(response.json()["sample_rows"]) == 3
    client.post("/api/fleet-health/backfill?dry_run=false&days=7")
    assert captured == {"days": 7, "dry_run": False}


def test_matching_is_visible_to_dispatcher_but_not_to_drivers(monkeypatch):
    assert client_as("dispatcher", monkeypatch)[0].get("/api/fleet-health/matching").status_code == 200
    assert client_as("driver", monkeypatch)[0].get("/api/fleet-health/matching").status_code == 403
