"""Phase 0 of the driver app / Overview rebuild: ONE read model, real pack counts, trips never merged, reconciliation.

Modelled on the real production data found on 2026-10-13: driver 13 had two separate Assign clicks (5 SOs / 564.67 kg and 1 SO / 100 kg)
on the same truck on the same day; every line unit was `kg` or `pck` (never "pack"). No Zoho call may happen (build_env fails the test).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from models.sales_order_lines import SalesOrderLine
from routers import driver as driver_router, driver_overview
from services import day_model, pack_units
from tests import trip_test_support as t


@pytest.fixture()
def env(monkeypatch):
    env = t.build_env(monkeypatch, driver_router.router, driver_overview.router)
    t.seed_templates(env)
    env.admin = t.make_user(env, name="Ada Admin", email="ada@example.test", role="admin")
    env.dispatcher = t.make_user(env, name="Dee Dispatcher", email="dee@example.test", role="dispatcher")
    env.emert = t.make_user(env, name="Emert", email="emert@example.test", role="driver", staff_id=9)
    env.h = {"admin": t.bearer(env.admin, "admin"), "dispatcher": t.bearer(env.dispatcher, "dispatcher"), "driver": t.bearer(env.emert, "driver")}
    yield env
    assert env.zoho_calls == []
    env.engine.dispose()


def add_lines(env, so_id, *lines):
    """lines: (unit, quantity, weight_kg)"""
    with env.Session() as db:
        for index, (unit, quantity, weight) in enumerate(lines):
            db.add(SalesOrderLine(sales_order_id=so_id, name=f"Item {index}", quantity=quantity, unit=unit, weight_kg=weight))
        db.commit()


def seed_two_trips(env):
    """Driver 9 + helper 17: batch T1 = 5 kg-only SOs (564.67 kg), batch T2 = 1 SO of 100 kg, same truck, 7 minutes apart.
    Driver 10: one batch of pack lines (`pck`), with an unknown weight on one line."""
    day = t.today()
    weights = [103.29, 150.0, 250.0, 20.0, 41.38]
    for i, kg in enumerate(weights):
        t.add_so(env, f"t1-{i}", number=f"SO-T1-{i}", batch="T1", driver=9, helpers=(17,), day=day, vehicle="DCD8954", offset=i)
        add_lines(env, f"t1-{i}", ("kg", kg, kg))
    t.add_so(env, "t2-0", number="SO-T2-0", batch="T2", driver=9, helpers=(17,), day=day, vehicle="DCD8954", offset=60)
    add_lines(env, "t2-0", ("kg", 100.0, 100.0))
    for i in range(2):
        t.add_so(env, f"p-{i}", number=f"SO-P-{i}", batch="P", driver=10, day=day, vehicle="BHENTZ", offset=100 + i)
    add_lines(env, "p-0", ("pck", 36.0, None), ("kg", 10.0, 10.0))
    add_lines(env, "p-1", ("pck", 30.0, 30.0), ("pc", 5.0, 5.0))
    # yesterday: must never show on Today
    t.add_so(env, "old-1", number="SO-OLD", batch="OLD", driver=9, day=day - timedelta(days=1), vehicle="DCD8954")
    add_lines(env, "old-1", ("kg", 999.0, 999.0))
    return day


def today_batches(env, who="driver"):
    response = env.client.get("/api/driver/today", headers=env.h[who])
    assert response.status_code == 200, response.text
    return response.json()["batches"]


# ---- pack rule ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("unit,expected", [("pck", True), ("PCK", True), ("pack", True), ("Packs", True), ("pcs", False), ("pc", False), ("kg", False), ("case", False), (None, False), ("", False)])
def test_is_pack_unit_matches_the_real_unit_codes(unit, expected):
    assert pack_units.is_pack_unit(unit) is expected


def test_pack_total_is_none_not_zero_when_no_line_is_in_packs():
    assert pack_units.pack_total([{"unit": "kg", "quantity": 12}]) is None
    assert pack_units.pack_total([]) is None
    assert pack_units.pack_total([{"unit": "pck", "quantity": 6}, {"unit": "pck", "quantity": 2.5}, {"unit": "kg", "quantity": 9}]) == 8.5


# ---- the two cards ----------------------------------------------------------------------------------------------------------


def test_two_assign_clicks_on_one_truck_are_two_numbered_trips_never_merged(env):
    seed_two_trips(env)
    batches = [b for b in today_batches(env) if b["vehicle"] == "DCD8954"]
    assert len(batches) == 2
    by_id = {b["batch_id"]: b for b in batches}
    assert by_id["T1"]["so_count"] == 5 and by_id["T1"]["total_kg"] == pytest.approx(564.67) and by_id["T1"]["trip_number"] == 1 and by_id["T1"]["trips_on_date"] == 2
    assert by_id["T2"]["so_count"] == 1 and by_id["T2"]["total_kg"] == 100.0 and by_id["T2"]["trip_number"] == 2


def test_today_only_shows_todays_shipment_date(env):
    seed_two_trips(env)
    numbers = {o for b in today_batches(env) for o in [b["batch_id"]]}
    assert "OLD" not in numbers  # yesterday's SO is History, never Today


def test_helper_sees_the_same_numbers_as_the_driver(env):
    seed_two_trips(env)
    helper = t.make_user(env, name="Raymond", email="ray@example.test", role="helper", staff_id=17)
    seen = {b["batch_id"]: (b["so_count"], b["total_kg"], b["total_packs"]) for b in env.client.get("/api/driver/today", headers=t.bearer(helper, "helper")).json()["batches"]}
    assert seen == {b["batch_id"]: (b["so_count"], b["total_kg"], b["total_packs"]) for b in today_batches(env)}


# ---- packs -------------------------------------------------------------------------------------------------------------------


def test_packs_are_none_when_there_is_no_pack_data_and_real_when_there_is(env):
    seed_two_trips(env)
    by_id = {b["batch_id"]: b for b in today_batches(env)}
    assert by_id["T1"]["total_packs"] is None and by_id["T2"]["total_packs"] is None  # kg-only orders: unknown, NOT 0
    orders = env.client.get("/api/driver/assignments/T1", headers=env.h["driver"]).json()["orders"]
    assert all(o["total_packs"] is None for o in orders)
    assert all(line["packs"] is None for o in orders for line in o["lines"])


def test_pck_lines_count_as_packs_and_unknown_kg_is_flagged(env):
    seed_two_trips(env)
    driver10 = t.make_user(env, name="Paul", email="paul@example.test", role="driver", staff_id=10)
    batch = env.client.get("/api/driver/today", headers=t.bearer(driver10, "driver")).json()["batches"][0]
    assert batch["total_packs"] == 66.0  # 36 + 30 (the `pc` line is pieces, the kg line is a weight)
    assert batch["total_kg"] == 45.0 and batch["kg_complete"] is False  # the 36-pack line has no known weight: a partial total is flagged


# ---- overview == app ---------------------------------------------------------------------------------------------------------


def test_overview_board_row_is_the_sum_of_all_of_the_drivers_trips_and_equals_the_app(env):
    seed_two_trips(env)
    rows = {r["staff_id"]: r for r in env.client.get("/api/driver-overview/board", headers=env.h["dispatcher"]).json()["rows"]}
    row = rows[9]
    assert row["batch_count"] == 2 and row["progress"] == {"done": 0, "total": 6}  # NOT "0 of 1"
    assert row["kg"] == pytest.approx(664.67) and row["packs"] is None
    assert [(trip["trip_number"], trip["batch_id"], trip["so_count"]) for trip in row["trips"]] == [(1, "T1", 5), (2, "T2", 1)]
    assert rows[17]["progress"] == row["progress"]  # the helper's row equals the driver's (same crew)
    app = today_batches(env)
    assert row["progress"]["total"] == sum(b["so_count"] for b in app if b["vehicle"] == "DCD8954")


def test_summary_kpis_use_the_same_totals(env):
    seed_two_trips(env)
    k = env.client.get("/api/driver-overview/summary", headers=env.h["dispatcher"]).json()["kpis"]
    assert k["sos"]["assigned"] == 8 and k["batches"] == 3 and k["packs"] == 66.0


def test_acknowledged_and_delivered_counts_agree_between_app_and_overview(env):
    day = seed_two_trips(env)
    trip_id = t.add_trip(env, "T1", status="on_trip", day=day, driver=9, helpers=(17,), vehicle="DCD8954", started=t.utc(day, 8))
    for i in range(5):
        t.add_ack(env, trip_id, "T1", f"t1-{i}", at=t.utc(day, 7, i), user_id=env.emert)
    t.add_stop(env, trip_id, "t1-0", 1, status="checked_out", checkin=t.utc(day, 9), checkout=t.utc(day, 9, 20), outcome="Delivered", user_id=env.emert)
    t.add_stop(env, trip_id, "t1-1", 2, status="checked_out", checkin=t.utc(day, 10), checkout=t.utc(day, 10, 20), outcome="Failed / Returned", reason="closed", user_id=env.emert)
    app = {b["batch_id"]: b for b in today_batches(env)}
    assert app["T1"]["progress"] == {"acknowledged": 5, "stops_done": 2, "delivered": 1, "partial": 0, "failed": 1}
    assert app["T1"]["trip"]["acknowledged"] == 5 and app["T1"]["trip"]["stops_done"] == 2
    row = {r["staff_id"]: r for r in env.client.get("/api/driver-overview/board", headers=env.h["dispatcher"]).json()["rows"]}[9]
    assert row["progress"] == {"done": 2, "total": 6} and row["acknowledged"] == 5 and row["state"] == "on_trip"
    k = env.client.get("/api/driver-overview/summary", headers=env.h["dispatcher"]).json()["kpis"]["sos"]
    assert (k["acknowledged"], k["delivered"], k["failed"]) == (5, 1, 1)


# ---- reconciliation ----------------------------------------------------------------------------------------------------------


def reconcile(env, query=""):
    response = env.client.get(f"/api/driver-overview/reconcile{query}", headers=env.h["admin"])
    assert response.status_code == 200, response.text
    return response.json()


def test_reconcile_is_admin_only(env):
    seed_two_trips(env)
    assert env.client.get("/api/driver-overview/reconcile", headers=env.h["dispatcher"]).status_code == 403
    assert env.client.get("/api/driver-overview/reconcile", headers=env.h["driver"]).status_code == 403
    assert env.client.get("/api/driver-overview/reconcile").status_code in (401, 403)


def test_reconcile_agrees_across_load_planning_driver_app_and_overview(env):
    seed_two_trips(env)
    report = reconcile(env, "?source=neon")
    assert report["ok"] is True, report
    people = {p["staff_id"]: p for p in report["people"]}
    for side in ("load_planning", "driver_app", "overview"):
        assert people[9][side] == {"sos": 6, "kg": pytest.approx(664.67), "packs": None, "batches": 2}
        assert people[17][side] == people[9][side]
        assert people[10][side]["packs"] == 66.0 and people[10][side]["sos"] == 2
    assert report["distinct_sos"] == 8 and report["overview_kpi_sos_assigned"] == 8 and report["load_planning_source"] == "neon"


def test_reconcile_reports_a_difference_instead_of_hiding_it(env, monkeypatch):
    seed_two_trips(env)
    real = day_model.order_totals
    monkeypatch.setattr(day_model, "order_totals", lambda lines: {**real(lines), "kg": real(lines)["kg"] + 1.0})  # a drifting second implementation
    report = reconcile(env, "?source=neon")
    assert report["ok"] is False
    assert any("kg" in p["mismatches"] for p in report["people"])


# ---- text encoding -----------------------------------------------------------------------------------------------------------


def test_text_repair_undoes_cp1252_decoded_middle_dot_and_apostrophe():
    from services.text_repair import repair_deep, repair_text

    broken_dot = "On trip " + "Â·" + " stop 2"
    assert repair_text(broken_dot) == "On trip · stop 2"
    broken_apostrophe = "Denny" + "â€™" + "s"
    assert repair_text(broken_apostrophe) == "Denny’s"
    assert repair_text("plain text · already fine") == "plain text · already fine"
    assert repair_deep({"a": [broken_dot], "b": 3}) == {"a": ["On trip · stop 2"], "b": 3}


def test_staff_directory_and_driver_payload_repair_mis_decoded_text(env):
    from services import staff_directory_cache

    staff = staff_directory_cache._to_staff({"id": 1, "staffName": "JosÃ© Cruz", "title": "DRIVER", "warehouseAssignment": "METS"})
    assert staff["name"] == "José Cruz"


def test_no_mojibake_and_every_text_file_is_plain_utf8_without_bom():
    """"Â·" is "·" decoded as cp1252. Nothing in the repo may contain it, and no text file may carry a BOM or be non-UTF-8."""
    import subprocess
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    roots = [backend, backend.parent]
    suspicious = ("Â·", "Ã‚Â", "â€", "Ã¢â")
    seen: set[Path] = set()
    for root in roots:
        listing = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout.splitlines()
        for name in listing:
            path = (root / name).resolve()
            if path in seen or path.suffix not in {".py", ".ts", ".tsx", ".md", ".css", ".html", ".json", ".txt", ".yaml", ".yml"} or not path.exists():
                continue
            seen.add(path)
            raw = path.read_bytes()
            assert not raw.startswith(b"\xef\xbb\xbf"), f"BOM in {path}"
            text = raw.decode("utf-8")  # raises on a non-UTF-8 file
            if path.name == Path(__file__).name:
                continue
            for needle in suspicious:
                assert needle not in text, f"mojibake {needle!r} in {path}"
