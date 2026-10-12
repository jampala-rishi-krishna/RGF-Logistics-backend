"""Live GPS stream: only changed fixes are broadcast, the device timestamp is carried through, the cadence stat counts
real timestamp advances, and Fleet's cache is built once however many /vehicles requests arrive. Everything is mocked:
no Cartrack, no Zoho, no database."""
import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from routers import fleet
from services import cartrack_poller, gps_cadence, live_gps_store, live_sales_order_cache

PLATE = "ABC1234"


def raw_status(event_ts="2026-10-12 15:00:00+08", lat=14.5, lng=121.0, speed=0, plate=PLATE):
    return {"vehicle_id": "1", "registration": plate, "event_ts": event_ts, "speed": speed, "bearing": 90,
            "ignition": speed > 0, "location": {"latitude": lat, "longitude": lng, "position_description": "somewhere"},
            "fuel": {"precentage_left": 50}}


@pytest.fixture(autouse=True)
def clean():
    live_gps_store._live.clear()
    gps_cadence.reset()
    yield
    live_gps_store._live.clear()
    gps_cadence.reset()


def run_poll(statuses):
    sent = []

    async def fake_statuses():
        return statuses

    async def fake_broadcast(payload):
        sent.append(payload)

    with patch.object(cartrack_poller, "get_vehicle_statuses", fake_statuses), \
            patch.object(cartrack_poller.fleet_static_cache, "plate_to_id", return_value={PLATE: 7}), \
            patch.object(cartrack_poller.manager, "broadcast", fake_broadcast):
        asyncio.run(cartrack_poller.poll_cartrack_and_update())
    run_poll.heartbeats = [p for p in sent if p["type"] == "GPS_HEARTBEAT"]
    return [p for p in sent if p["type"] == "VEHICLE_POSITION_UPDATE"]


def test_unchanged_fix_is_not_broadcast_but_a_changed_one_is():
    assert len(run_poll([raw_status()])) == 1  # first sighting
    assert run_poll([raw_status()]) == []  # identical position and device timestamp: nothing to push
    moved = run_poll([raw_status(event_ts="2026-10-12 15:00:15+08", lat=14.5004, speed=30)])
    assert len(moved) == 1 and moved[0]["current_lat"] == 14.5004


def test_a_heartbeat_goes_out_every_poll_even_when_nothing_moved():
    run_poll([raw_status()])
    assert run_poll([raw_status()]) == []
    assert len(run_poll.heartbeats) == 1 and run_poll.heartbeats[0]["changed"] == 0 and run_poll.heartbeats[0]["matched"] == 1


def test_broadcast_carries_the_device_timestamp_not_the_poll_time():
    update = run_poll([raw_status(event_ts="2026-10-12 15:00:00+08")])[0]
    assert update["gps_time"] == "2026-10-12T07:00:00+00:00"  # +08 device clock -> UTC
    assert update["last_updated"] != update["gps_time"]  # last_updated stays the server's poll time


def test_parked_truck_repeating_one_event_ts_stays_silent_for_many_polls():
    run_poll([raw_status()])
    assert sum(len(run_poll([raw_status()])) for _ in range(10)) == 0
    stats = gps_cadence.snapshot()
    assert stats["broadcasts_sent"] == 1 and stats["broadcasts_skipped_unchanged"] == 10


def test_last_updated_still_refreshes_when_the_fix_is_unchanged():
    run_poll([raw_status()])
    first = live_gps_store.all_entries()[PLATE]["last_updated"]
    time.sleep(0.01)
    run_poll([raw_status()])
    assert live_gps_store.all_entries()[PLATE]["last_updated"] > first


def test_cadence_measures_gaps_between_device_timestamps_split_by_moving_and_parked():
    for ts, speed in (("2026-10-12 15:00:00+08", 40), ("2026-10-12 15:00:10+08", 40), ("2026-10-12 15:00:20+08", 40),
                      ("2026-10-12 15:00:20+08", 40), ("2026-10-12 15:10:20+08", 0)):
        run_poll([raw_status(event_ts=ts, speed=speed, lat=14.5 + speed / 1e5)])
    vehicle = gps_cadence.snapshot()["vehicles"][PLATE]
    assert vehicle["gps_time_field"] == "event_ts"
    assert vehicle["moving_gap"]["median_s"] == 10.0 and vehicle["moving_gap"]["samples"] == 2
    assert vehicle["parked_gap"]["median_s"] == 600.0
    assert vehicle["timestamp_advances"] == 3  # the repeated 15:00:20 is not an advance


def test_unparseable_timestamp_still_updates_the_store_without_breaking_the_stream():
    sent = run_poll([raw_status(event_ts="garbage")])
    assert len(sent) == 1 and sent[0]["gps_time"] is None


# --- Fleet cache single flight ---------------------------------------------------------------------------------

@pytest.fixture
def cold_fleet():
    fleet._fleet_cache, fleet._fleet_cache_built = [], False
    fleet._stale_fleet_cache, fleet._build_generation = [], 0
    yield
    fleet._fleet_cache, fleet._fleet_cache_built = [], False
    fleet._stale_fleet_cache, fleet._build_generation = [], 0


def fake_db():
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = [SimpleNamespace(plate_no=PLATE)]
    return db


def payload(vehicle, db, **_kwargs):
    return {"plate_no": vehicle.plate_no, "associated_sos": []}


def test_20_concurrent_vehicle_requests_during_cold_start_make_one_zoho_build(cold_fleet):
    builds = []

    def slow_snapshot(include_window=True, statuses=None):
        builds.append(include_window)
        time.sleep(0.3)  # long enough that all 20 requests overlap the build
        return [], False

    results, errors = [], []

    def request():
        try:
            results.append(fleet.list_vehicles(status=None, date=None, db=fake_db()))
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    with patch.object(live_sales_order_cache, "get_assigned_snapshot_ex", slow_snapshot), \
            patch.object(fleet, "_vehicle_payload", payload):
        threads = [threading.Thread(target=request) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert errors == []
    assert len(builds) == 1, f"expected ONE Zoho build for 20 concurrent requests, got {len(builds)}"
    assert builds == [False]  # assigned SOs only, never the whole shipment window
    assert len(results) == 20 and all(len(r) == 1 and r[0]["plate_no"] == PLATE for r in results)


def test_built_cache_serves_later_requests_without_any_zoho_call(cold_fleet):
    calls = []
    with patch.object(live_sales_order_cache, "get_assigned_snapshot_ex", lambda include_window=True, statuses=None: (calls.append(1) or [], False)), \
            patch.object(fleet, "_vehicle_payload", payload):
        for _ in range(5):
            fleet.list_vehicles(status=None, date=None, db=fake_db())
    assert len(calls) == 1


def test_readers_get_the_last_good_snapshot_while_a_rebuild_is_running(cold_fleet):
    started, release = threading.Event(), threading.Event()

    def snapshot(include_window=True, statuses=None):
        started.set()
        release.wait(5)
        return [], False

    with patch.object(live_sales_order_cache, "get_assigned_snapshot_ex", lambda include_window=True, statuses=None: ([], False)), \
            patch.object(fleet, "_vehicle_payload", payload):
        fleet.list_vehicles(status=None, date=None, db=fake_db())  # builds the first snapshot
        fleet.invalidate_fleet_cache()
    with patch.object(live_sales_order_cache, "get_assigned_snapshot_ex", snapshot), patch.object(fleet, "_vehicle_payload", payload):
        rebuild = threading.Thread(target=lambda: fleet.list_vehicles(status=None, date=None, db=fake_db()))
        rebuild.start()
        assert started.wait(5)
        began = time.monotonic()
        stale = fleet.list_vehicles(status=None, date=None, db=fake_db())  # must not wait for the rebuild
        assert time.monotonic() - began < 1.0
        assert [v["plate_no"] for v in stale] == [PLATE]
        release.set()
        rebuild.join(5)


def test_failed_assigned_fetch_is_not_latched_and_the_next_request_retries(cold_fleet):
    outcomes = iter([([], True), ([], False)])
    with patch.object(live_sales_order_cache, "get_assigned_snapshot_ex", lambda include_window=True, statuses=None: next(outcomes)), \
            patch.object(fleet, "_vehicle_payload", payload):
        fleet.list_vehicles(status=None, date=None, db=fake_db())
        assert fleet._fleet_cache_built is False
        fleet.list_vehicles(status=None, date=None, db=fake_db())
        assert fleet._fleet_cache_built is True


def test_assigned_only_snapshot_never_pulls_the_shipment_window():
    with patch.object(live_sales_order_cache, "get_window", side_effect=AssertionError("window pulled")) as window, \
            patch.object(live_sales_order_cache, "_assignment_state", {"1": {"assignment_status": "assigned"}}), \
            patch.object(live_sales_order_cache, "_assigned_zoho", {"1": SimpleNamespace(id="1")}):
        rows, failed = live_sales_order_cache.get_assigned_snapshot_ex(include_window=False)
    assert window.call_count == 0 and failed is False and [r.id for r in rows] == ["1"]


# --- LOCAL_DISABLE_ZOHO_WARMUP -----------------------------------------------------------------------------------

def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("LOCAL_DISABLE_ZOHO_WARMUP", raising=False)
    assert live_sales_order_cache.local_zoho_warmup_disabled() is False


@pytest.mark.parametrize("value", ["true", "1", "YES"])
def test_flag_on_means_fleet_build_makes_zero_zoho_calls(cold_fleet, monkeypatch, value):
    monkeypatch.setenv("LOCAL_DISABLE_ZOHO_WARMUP", value)
    with patch.object(live_sales_order_cache, "get_assigned_snapshot_ex", side_effect=AssertionError("Zoho path used")), \
            patch.object(live_sales_order_cache, "cached_assigned_records", return_value=[]), \
            patch.object(fleet, "_vehicle_payload", payload):
        result = fleet.list_vehicles(status=None, date=None, db=fake_db())
    assert [v["plate_no"] for v in result] == [PLATE]
    assert fleet._fleet_cache_built is True


def test_flag_on_skips_startup_warmup_and_scheduled_sync(cold_fleet, monkeypatch):
    monkeypatch.setenv("LOCAL_DISABLE_ZOHO_WARMUP", "true")
    with patch.object(fleet, "SessionLocal", side_effect=AssertionError("db opened")), \
            patch.object(fleet, "refresh_vehicles", side_effect=AssertionError("sync ran")):
        fleet.warm_fleet_cache()
        fleet.scheduled_fleet_sync()


# --- Fleet fetches assigned SOs only ---------------------------------------------------------------------------

def history_state(assigned=12, manifested=5, completed=40, released=10, unassigned=17):
    """The 84-row example from the production startup log, split into plausible assignment states."""
    state, n = {}, 0
    for status, count in (("assigned", assigned), ("manifested", manifested), ("completed", completed),
                          ("released", released), ("unassigned", unassigned)):
        for _ in range(count):
            n += 1
            state[f"so{n}"] = {"assignment_status": status, "vehicle_id": PLATE}
    return state


def counting_ensure(fetched):
    def ensure(order_id):
        fetched.append(order_id)
        row = SimpleNamespace(id=order_id, assignment_status="assigned", vehicle_id=PLATE)
        live_sales_order_cache._assigned_zoho[order_id] = row
        return row
    return ensure


def test_cold_start_fetches_only_the_assigned_rows_of_the_84_row_history():
    state, fetched = history_state(), []
    assert len(state) == 84
    with patch.object(live_sales_order_cache, "_assignment_state", state), \
            patch.object(live_sales_order_cache, "_assigned_zoho", {}), \
            patch.object(live_sales_order_cache, "_excluded_ids", {}), \
            patch.object(live_sales_order_cache, "get_window", side_effect=AssertionError("window pulled")), \
            patch.object(live_sales_order_cache, "ensure_zoho_data", counting_ensure(fetched)):
        rows, failed = live_sales_order_cache.get_assigned_snapshot_ex(include_window=False, statuses=fleet.ASSIGNED_ONLY)
    assert len(fetched) == 12 and failed is False and len(rows) == 12
    assert all(state[oid]["assignment_status"] == "assigned" for oid in fetched)


def test_without_the_status_filter_every_non_unassigned_row_would_be_fetched():
    """Documents the saving: the previous behaviour cost 67 detail calls for the same 84 rows."""
    state, fetched = history_state(), []
    with patch.object(live_sales_order_cache, "_assignment_state", state), \
            patch.object(live_sales_order_cache, "_assigned_zoho", {}), \
            patch.object(live_sales_order_cache, "_excluded_ids", {}), \
            patch.object(live_sales_order_cache, "ensure_zoho_data", counting_ensure(fetched)):
        live_sales_order_cache.get_assigned_snapshot_ex(include_window=False)
    assert len(fetched) == 67


def test_rows_already_in_memory_cost_nothing():
    state, fetched = history_state(assigned=3, manifested=0, completed=0, released=0, unassigned=0), []
    warm = {oid: SimpleNamespace(id=oid, assignment_status="assigned", vehicle_id=PLATE) for oid in state}
    with patch.object(live_sales_order_cache, "_assignment_state", state), patch.object(live_sales_order_cache, "_assigned_zoho", warm), \
            patch.object(live_sales_order_cache, "_excluded_ids", {}), patch.object(live_sales_order_cache, "ensure_zoho_data", counting_ensure(fetched)):
        rows, _ = live_sales_order_cache.get_assigned_snapshot_ex(include_window=False, statuses=fleet.ASSIGNED_ONLY)
    assert fetched == [] and len(rows) == 3


def test_fleet_build_asks_for_assigned_only_without_the_window(cold_fleet):
    seen = {}

    def snapshot(include_window=True, statuses=None):
        seen.update(include_window=include_window, statuses=statuses)
        return [], False

    with patch.object(live_sales_order_cache, "get_assigned_snapshot_ex", snapshot), patch.object(fleet, "_vehicle_payload", payload):
        fleet.list_vehicles(status=None, date=None, db=fake_db())
    assert seen == {"include_window": False, "statuses": frozenset({"assigned"})}


def test_date_scoped_fleet_view_asks_for_assigned_only_without_the_window():
    from datetime import datetime, timedelta

    seen = {}

    def snapshot(include_window=True, statuses=None):
        seen.update(include_window=include_window, statuses=statuses)
        return [], False

    tomorrow = (datetime.now(fleet.OPS_TZ).date() + timedelta(days=1)).isoformat()
    with patch.object(live_sales_order_cache, "get_assigned_snapshot_ex", snapshot), patch.object(fleet, "_vehicle_payload", payload):
        result = fleet.list_vehicles(status=None, date=tomorrow, db=fake_db())
    assert seen == {"include_window": False, "statuses": frozenset({"assigned"})}
    assert [v["plate_no"] for v in result] == [PLATE]


def test_date_scoped_view_still_filters_to_the_requested_delivery_date():
    from datetime import date, datetime, timedelta

    tomorrow = datetime.now(fleet.OPS_TZ).date() + timedelta(days=1)
    mine = SimpleNamespace(id="a", assignment_status="assigned", vehicle_id=PLATE, expected_shipment_date=tomorrow, assigned_at=None, salesorder_number="SO-A")
    other_day = SimpleNamespace(id="b", assignment_status="assigned", vehicle_id=PLATE, expected_shipment_date=date(2030, 1, 1), assigned_at=None, salesorder_number="SO-B")
    seen = {}

    def capture(vehicle, db, assigned_by_vehicle=None, **_):
        seen["orders"] = [o.id for o in assigned_by_vehicle.get(PLATE, [])]
        return {"plate_no": vehicle.plate_no, "associated_sos": []}

    with patch.object(live_sales_order_cache, "get_assigned_snapshot_ex", lambda include_window=True, statuses=None: ([mine, other_day], False)), \
            patch.object(fleet, "_vehicle_payload", capture):
        fleet.list_vehicles(status=None, date=tomorrow.isoformat(), db=fake_db())
    assert seen["orders"] == ["a"]


def test_startup_log_breaks_the_loaded_history_down_by_status(caplog):
    rows = [SimpleNamespace(id=f"so{i}", vehicle_id=PLATE, driver_id=None, assignment_status=status, assigned_at=None, assigned_by=None,
                            completed_at=None, route_id=None, manifest_id=None, helper_ids=[], email_status=None, email_error=None,
                            email_sent_at=None, email_message_id=None, assignment_batch_id=None)
            for i, status in enumerate(["assigned", "assigned", "completed", "released"])]
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = rows
    with patch.object(live_sales_order_cache, "_assignment_state", {}), caplog.at_level("INFO", logger=live_sales_order_cache.logger.name):
        live_sales_order_cache.load_assignment_state_from_history(db)
    assert "Loaded assignment state for 4 SOs" in caplog.text
    assert "'assigned': 2" in caplog.text and "'completed': 1" in caplog.text and "'released': 1" in caplog.text
