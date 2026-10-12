"""Driver Overview: KPIs, board, person detail, timeline, map, CSV export, template versioning, caching. Zero Zoho."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select

from models.trip import FormTemplate, FormTemplateItem
from routers import driver_overview
from services import trip_events
from tests import trip_test_support as t

DAY = None  # set per test via t.today()


@pytest.fixture()
def env(monkeypatch):
    env = t.build_env(monkeypatch, driver_overview.router)
    t.seed_templates(env)
    env.admin = t.make_user(env, name="Ada Admin", email="ada@example.test", role="admin")
    env.dispatcher = t.make_user(env, name="Dee Dispatcher", email="dee@example.test", role="dispatcher")
    env.driver_user = t.make_user(env, name="Emert", email="emert@example.test", role="driver", staff_id=9)
    env.officer = t.make_user(env, name="Olivia Officer", email="olivia@example.test", role="logistics_officer")
    env.h = {"admin": t.bearer(env.admin, "admin"), "dispatcher": t.bearer(env.dispatcher, "dispatcher"), "driver": t.bearer(env.driver_user, "driver"), "officer": t.bearer(env.officer, "logistics_officer")}
    yield env
    assert env.zoho_calls == []
    env.engine.dispose()


def seed(env):
    """Today: A (driver 9 GLACIER + helper 17) on_trip; B (driver 10 METS + helper 18) handover_submitted; C (driver 11 METS, helper 18) not acknowledged.
    Yesterday: E (driver 9) returned with one missing invoice. Staff 12 is an idle Glacier driver, 13 is inactive."""
    day, yesterday = t.today(), t.today() - timedelta(days=1)
    now = datetime.now(timezone.utc)
    uid = env.driver_user

    # --- A: on trip, stop 1 delivered, stop 2 checked in (no GPS), stop 3 pending; last event 40 minutes ago ---
    for i in (1, 2, 3):
        t.add_so(env, f"a{i}", number=f"SO-A{i}", customer=f"Alpha {i}", batch="A", driver=9, helpers=(17,), day=day, offset=i)
    a = t.add_trip(env, "A", status="on_trip", day=day, driver=9, helpers=(17,), started=t.utc(day, 8), warehouse="GLACIER")
    for i in (1, 2, 3):
        t.add_ack(env, a, "A", f"a{i}", at=t.utc(day, 7, i), user_id=uid)
    t.add_stop(env, a, "a1", 1, number="SO-A1", customer="Alpha 1", status="checked_out", checkin=t.utc(day, 9), checkout=t.utc(day, 9, 25), outcome="Delivered", user_id=uid, distance=120.0, far=False,
               answers=[{"key": "outcome", "label": "Delivery outcome", "value": "Delivered"}, {"key": "received_by", "label": "Received by", "value": "Ana"}, {"key": "delivery_photo", "label": "Photo", "value": 12}])
    t.add_stop(env, a, "a2", 2, number="SO-A2", customer="Alpha 2", status="checked_in", checkin=t.utc(day, 10), user_id=uid, gps=False)
    t.add_stop(env, a, "a3", 3, number="SO-A3", customer="Alpha 3")
    t.add_event(env, a, "A", "trip_started", now - timedelta(minutes=120), user_id=uid, gps={"lat": 14.49, "lng": 120.99, "accuracy_m": 8, "reason": None})
    t.add_event(env, a, "A", "checked_in", now - timedelta(minutes=40), user_id=uid, so_id="a2", gps={"lat": None, "lng": None, "accuracy_m": None, "reason": "denied"})

    # --- B: returned and handed over, three outcomes ---
    for i in (1, 2, 3):
        t.add_so(env, f"b{i}", number=f"SO-B{i}", customer=f"Beta {i}", batch="B", driver=10, helpers=(18,), day=day, vehicle="NBB5678", offset=10 + i)
    b = t.add_trip(env, "B", status="handover_submitted", day=day, driver=10, helpers=(18,), vehicle="NBB5678", warehouse="METS", started=t.utc(day, 7), ended=t.utc(day, 14))
    for i, outcome in enumerate(("Delivered", "Partially delivered", "Failed / Returned"), 1):
        t.add_ack(env, b, "B", f"b{i}", at=t.utc(day, 6, i), user_id=uid)
        t.add_stop(env, b, f"b{i}", i, number=f"SO-B{i}", customer=f"Beta {i}", status="checked_out", checkin=t.utc(day, 8 + i), checkout=t.utc(day, 8 + i, 10), outcome=outcome, reason=None if i == 1 else "short", user_id=uid)
    t.add_handover(env, b, "B", [(f"b{i}", f"SO-B{i}", f"Beta {i}", True, "pending") for i in (1, 2, 3)], status="pending", officer_user_id=env.officer)
    t.add_event(env, b, "B", "handover_submitted", now - timedelta(minutes=2), user_id=uid)

    # --- C: assigned, nothing done ---
    for i in (1, 2):
        t.add_so(env, f"c{i}", number=f"SO-C{i}", customer=f"Gamma {i}", batch="C", driver=11, helpers=(18,), day=day, vehicle="NCC9999", offset=20 + i)

    # --- E: yesterday, returned, one invoice missing (outstanding counts it on every date range) ---
    t.add_so(env, "e1", number="SO-E1", customer="Epsilon", batch="E", driver=9, day=yesterday, offset=30)
    e = t.add_trip(env, "E", status="returned", day=yesterday, driver=9, started=t.utc(yesterday, 8), ended=t.utc(yesterday, 16), warehouse="GLACIER")
    t.add_stop(env, e, "e1", 1, number="SO-E1", customer="Epsilon", status="checked_out", checkin=t.utc(yesterday, 9), checkout=t.utc(yesterday, 9, 30), outcome="Delivered", user_id=uid)
    t.add_handover(env, e, "E", [("e1", "SO-E1", "Epsilon", True, "missing")], status="acknowledged", officer_user_id=env.officer)
    return day, yesterday


def kpis(env, query="", who="dispatcher"):
    response = env.client.get(f"/api/driver-overview/summary{query}", headers=env.h[who])
    assert response.status_code == 200, response.text
    return response.json()["kpis"]


def test_kpis_for_today_and_a_range(env):
    seed(env)
    k = kpis(env)
    assert k["drivers"] == {"active": 3, "on_trip": 1, "returned": 1, "idle": 1}  # idle: staff 12 only (13 is inactive, helpers never count)
    assert k["sos"] == {"assigned": 8, "acknowledged": 6, "delivered": 2, "partial": 1, "failed": 1}
    assert k["trips_pending_handover"] == 1
    assert k["invoices_outstanding"] == 4  # B's three pending + E's missing one (yesterday): all dates
    yesterday = (t.today() - timedelta(days=1)).isoformat()
    wide = kpis(env, f"?from={yesterday}&to={t.today().isoformat()}")
    assert wide["drivers"]["active"] == 3 and wide["drivers"]["on_trip"] == 1 and wide["drivers"]["returned"] == 1  # driver 9 has A (on trip) and E
    assert wide["sos"]["assigned"] == 9 and wide["trips_pending_handover"] == 2 and wide["invoices_outstanding"] == 4
    only_yesterday = kpis(env, f"?date={yesterday}")
    assert only_yesterday["drivers"] == {"active": 1, "on_trip": 0, "returned": 1, "idle": 3}
    assert only_yesterday["invoices_outstanding"] == 4  # not affected by the range
    summary = env.client.get(f"/api/driver-overview/summary?date={yesterday}", headers=env.h["admin"]).json()
    assert summary["from"] == summary["to"] == yesterday and summary["generated_at"]


def test_warehouse_and_driver_filters(env):
    seed(env)
    glacier = kpis(env, "?warehouse=GLACIER")
    assert glacier["drivers"] == {"active": 1, "on_trip": 1, "returned": 0, "idle": 1} and glacier["sos"]["assigned"] == 3
    assert glacier["invoices_outstanding"] == 1  # only E's missing invoice belongs to a Glacier driver
    mets = kpis(env, "?warehouse=mets")
    assert mets["drivers"] == {"active": 2, "on_trip": 0, "returned": 1, "idle": 0} and mets["sos"] == {"assigned": 5, "acknowledged": 3, "delivered": 1, "partial": 1, "failed": 1}
    assert mets["trips_pending_handover"] == 1 and mets["invoices_outstanding"] == 3
    one = kpis(env, "?driver_id=10")
    assert one["drivers"]["active"] == 1 and one["sos"]["assigned"] == 3
    assert env.client.get("/api/driver-overview/summary?warehouse=MOON", headers=env.h["admin"]).status_code == 422
    assert env.client.get("/api/driver-overview/summary?from=2026-02-01&to=2026-01-01", headers=env.h["admin"]).status_code == 422
    assert env.client.get("/api/driver-overview/summary?date=notadate", headers=env.h["admin"]).status_code == 422


def test_board_one_row_per_person_priority_stale_and_last_gps(env):
    seed(env)
    rows = env.client.get("/api/driver-overview/board", headers=env.h["dispatcher"]).json()["rows"]
    by_id = {r["staff_id"]: r for r in rows}
    assert len(rows) == len(by_id) == 6  # 9, 17, 10, 18, 11 + idle 12
    assert by_id[9]["state"] == "on_trip" and by_id[9]["role"] == "driver" and by_id[17]["role"] == "helper" and by_id[17]["state"] == "on_trip"
    assert by_id[9]["current_stop"] == "Alpha 2" and by_id[9]["progress"] == {"done": 1, "total": 3} and by_id[9]["vehicle"] == "NAA1234" and by_id[9]["batch_id"] == "A"
    assert by_id[9]["stale"] is True  # on trip, last event 40 min ago (default 15)
    assert by_id[9]["last_action"]["type"] == "checked_in" and "Arrived at SO-A2" == by_id[9]["last_action"]["label"]
    assert by_id[9]["last_gps_at"] is not None  # the trip_started event had GPS; the later one was denied
    assert by_id[10]["state"] == "handover_pending" and by_id[10]["stale"] is False and by_id[10]["last_gps_at"] is None
    assert by_id[11]["state"] == "to_acknowledge" and by_id[11]["progress"] == {"done": 0, "total": 2} and by_id[11]["last_action"] is None
    assert by_id[18]["state"] == "to_acknowledge" and by_id[18]["batch_id"] == "C"  # helper on B (handover_pending) and C (to_acknowledge): most active wins
    assert by_id[12]["state"] == "idle" and by_id[12]["batch_id"] is None and 13 not in by_id
    assert [r["state"] for r in rows][0] == "on_trip" and rows[-1]["state"] == "idle"
    relaxed = env.client.get("/api/driver-overview/board?stale_minutes=60", headers=env.h["admin"]).json()
    assert relaxed["stale_minutes"] == 60 and {r["staff_id"]: r for r in relaxed["rows"]}[9]["stale"] is False
    on_trip = env.client.get("/api/driver-overview/board?status=on_trip", headers=env.h["admin"]).json()["rows"]
    assert {r["staff_id"] for r in on_trip} == {9, 17}
    glacier = env.client.get("/api/driver-overview/board?warehouse=GLACIER", headers=env.h["admin"]).json()["rows"]
    assert {r["staff_id"] for r in glacier} == {9, 12}  # driver 17 (helper) belongs to METS in the staff record
    assert [r["staff_id"] for r in env.client.get("/api/driver-overview/board?driver_id=10", headers=env.h["admin"]).json()["rows"]] == [10]
    assert env.client.get("/api/driver-overview/board?status=flying", headers=env.h["admin"]).status_code == 422


def test_driver_detail_totals_and_batches(env):
    seed(env)
    yesterday = (t.today() - timedelta(days=1)).isoformat()
    body = env.client.get(f"/api/driver-overview/drivers/9?from={yesterday}&to={t.today().isoformat()}", headers=env.h["dispatcher"]).json()
    assert body["person"]["name"] == "Emert Hirondo" and body["person"]["role"] == "driver" and body["person"]["warehouse"] == "GLACIER"
    assert [b["batch_id"] for b in body["batches"]] == ["A", "E"]  # newest first
    assert body["batches"][0]["trip"]["chip"] == "on_trip" and body["batches"][0]["trip"]["chip_label"] == "On trip · stop 2 of 3" and body["batches"][1]["trip"]["handover_status"] == "acknowledged"
    totals = body["totals"]
    assert totals["batches"] == 2 and totals["sos"] == 4 and totals["delivered"] == 2 and totals["trips_completed"] == 1 and totals["trip_minutes"] == 480.0
    assert totals["avg_minutes_at_stop"] == 27.5 and totals["invoices_outstanding"] == 1  # 25 and 30 minutes at the two completed stops
    helper = env.client.get("/api/driver-overview/drivers/17", headers=env.h["admin"]).json()
    assert helper["person"]["role"] == "helper" and [b["role"] for b in helper["batches"]] == ["helper"]
    assert env.client.get("/api/driver-overview/drivers/4242", headers=env.h["admin"]).status_code == 404


def test_trip_timeline_content(env):
    seed(env)
    response = env.client.get("/api/driver-overview/trips/A", headers=env.h["dispatcher"])
    assert response.status_code == 200
    tl = response.json()
    assert tl["batch"]["vehicle"] == "NAA1234" and tl["batch"]["so_count"] == 3
    assert [(c["role"], c["name"]) for c in tl["crew"]] == [("driver", "Emert Hirondo"), ("helper", "Raymond Roblo")]
    assert tl["trip"]["status"] == "on_trip" and tl["trip"]["chip"] == "on_trip" and tl["trip"]["acknowledged"] == 3 and tl["trip"]["stops_done"] == 1 and tl["trip"]["duration_minutes"] is None
    first = tl["orders"][0]
    assert first["so_number"] == "SO-A1" and first["customer_name"] == "Alpha 1"
    labels = {a["label"]: a for a in first["ack"]["answers"]}
    assert labels["Products match the SO lines and quantities"]["value"] is True and labels["Products match the SO lines and quantities"]["type"] == "checkbox"
    assert labels["Number of packs received"]["value"] == 12 and labels["Notes / discrepancies"]["value"] == "all good" and labels["Notes / discrepancies"]["note"] == "short by 0"
    assert first["ack"]["template_version"] == 1 and first["ack"]["by"]["name"] == "Emert"
    stop = first["stop"]
    assert stop["sequence"] == 1 and stop["status"] == "checked_out" and stop["minutes_at_stop"] == 25.0
    assert stop["checkin"]["distance_m"] == 120.0 and stop["checkin"]["far"] is False and stop["checkin"]["gps"]["lat"] is not None
    assert stop["checkout"]["outcome"] == "Delivered" and stop["checkout"]["template_version"] == 1
    answers = {a["label"]: a for a in stop["checkout"]["answers"]}
    assert answers["Received by (name of the person at the customer)"]["value"] == "Ana"  # label comes from the stored template version
    assert answers["Photo of the delivery"]["photo_url"] == "/api/media/12" and answers["Photo of the delivery"]["value"] == 12
    second = tl["orders"][1]["stop"]
    assert second["status"] == "checked_in" and second["checkout"] is None and second["minutes_at_stop"] is None and second["checkin"]["gps"]["reason"] == "denied"
    assert tl["orders"][2]["stop"]["checkin"] is None
    assert [e["type"] for e in tl["events"]] == ["trip_started", "checked_in"] and tl["events"][1]["so_number"] == "SO-A2" and tl["events"][0]["by"]["name"] == "Emert"
    done = env.client.get("/api/driver-overview/trips/B", headers=env.h["admin"]).json()
    assert done["trip"]["duration_minutes"] == 420.0 and done["trip"]["trip_minutes"] == 420.0 and done["trip"]["handover_status"] == "pending"
    assert done["orders"][0]["handover"]["officer_status"] == "pending" and done["orders"][2]["stop"]["checkout"]["outcome"] == "Failed / Returned"
    untouched = env.client.get("/api/driver-overview/trips/C", headers=env.h["admin"]).json()  # no trip row yet: built from the assignment
    assert untouched["trip"]["status"] == "pending" and untouched["trip"]["chip"] == "to_acknowledge" and untouched["orders"][0]["ack"] is None and untouched["orders"][0]["stop"] is None
    assert env.client.get("/api/driver-overview/trips/nope", headers=env.h["admin"]).status_code == 404


def test_timeline_uses_the_template_version_stored_on_the_ack(env):
    """An acknowledgement filled with template v1 keeps v1's labels after v2 is published."""
    seed(env)
    new_items = [{"key": "only_item", "label": "A brand new question", "item_type": "checkbox", "required": True}]
    assert env.client.post("/api/driver-overview/templates/acknowledge_checklist", headers=env.h["admin"], json={"name": "Real checklist", "items": new_items}).status_code == 201
    tl = env.client.get("/api/driver-overview/trips/A", headers=env.h["admin"]).json()
    assert tl["orders"][0]["ack"]["template_version"] == 1
    assert "Products match the SO lines and quantities" in {a["label"] for a in tl["orders"][0]["ack"]["answers"]}


def test_map_has_only_points_with_gps(env):
    seed(env)
    body = env.client.get("/api/driver-overview/map", headers=env.h["dispatcher"]).json()
    assert [w["id"] for w in body["warehouses"]] == ["mets", "glacier"] and all(w["lat"] and w["lng"] for w in body["warehouses"])
    points = body["points"]
    assert len(points) == 11 and all(p["lat"] is not None and p["lng"] is not None for p in points)
    kinds = sorted(p["type"] for p in points if p["batch_id"] == "A")
    assert kinds == ["checkin", "checkout", "start"]  # A's stop 2 check-in had no GPS: not a point
    outcomes = {p["so_number"]: p["outcome"] for p in points if p["type"] == "checkout"}
    assert outcomes["SO-B2"] == "Partially delivered" and any(p["type"] == "end" and p["batch_id"] == "B" for p in points)
    assert {p["driver_name"] for p in points} == {"Emert Hirondo", "Paul Aljecera"}
    assert len(env.client.get("/api/driver-overview/map?driver_id=9", headers=env.h["admin"]).json()["points"]) == 3
    assert len(env.client.get("/api/driver-overview/map?warehouse=METS", headers=env.h["admin"]).json()["points"]) == 8


def test_csv_export_quoting_bom_and_local_times(env):
    day, _ = seed(env)
    t.add_so(env, "x1", number="SO-X1", customer='Acme, "Best" Foods\nMakati', batch="X", driver=11, day=day, vehicle="NCC9999")
    t.add_so(env, "x2", number="SO-X2", customer="=HYPERLINK(\"http://evil\")", batch="X", driver=11, day=day, vehicle="NCC9999", offset=1)
    response = env.client.get("/api/driver-overview/export.csv", headers=env.h["dispatcher"])
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv") and "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    header, body = rows[0], rows[1:]
    assert header == ["date", "batch", "driver", "helpers", "vehicle", "so_number", "customer", "acknowledged_at", "acknowledged_by", "checkin_at", "distance_m", "checkout_at", "outcome", "reason",
                      "minutes_at_stop", "trip_started", "trip_ended", "handover_status", "invoice_status", "officer", "validated_at"]
    assert len(body) == 10 and all(len(r) == len(header) for r in body)
    by_so = {r[5]: dict(zip(header, r)) for r in body}
    assert by_so["SO-X1"]["customer"] == 'Acme, "Best" Foods\nMakati'  # round-trips through proper quoting
    assert by_so["SO-X2"]["customer"].startswith("'=")  # formula injection neutralised
    a1 = by_so["SO-A1"]
    assert a1["driver"] == "Emert Hirondo" and a1["helpers"] == "Raymond Roblo" and a1["acknowledged_by"] == "Emert" and a1["distance_m"] == "120.0" and a1["minutes_at_stop"] == "25.0"
    assert a1["checkin_at"].endswith("+08:00") and a1["checkin_at"].startswith(f"{day.isoformat()}T09:00:00")
    assert a1["outcome"] == "Delivered" and a1["trip_started"].startswith(f"{day.isoformat()}T08:00:00") and a1["trip_ended"] == ""
    b2 = by_so["SO-B2"]
    assert b2["handover_status"] == "pending" and b2["invoice_status"] == "pending" and b2["officer"] == "" and b2["reason"] == "short"
    assert by_so["SO-C1"]["checkin_at"] == "" and by_so["SO-C1"]["handover_status"] == ""
    only_driver = env.client.get("/api/driver-overview/export.csv?driver_id=10", headers=env.h["admin"]).content.decode("utf-8-sig")
    assert len(list(csv.reader(io.StringIO(only_driver)))) == 4


def good_item(key, **extra):
    return {"key": key, "label": key.title(), "item_type": "text", "required": False, **extra}


def test_template_versioning_via_post(env):
    before = env.client.get("/api/driver-overview/templates", headers=env.h["dispatcher"]).json()
    assert before["delivery_checkout"]["active"]["version"] == 1 and before["delivery_checkout"]["active"]["is_draft"] is True
    assert [v["version"] for v in before["acknowledge_checklist"]["versions"]] == [1]
    items = [good_item("outcome", item_type="dropdown", options=["Delivered", "Failed / Returned"], required=True),
             good_item("reason", item_type="textarea", show_when={"key": "outcome", "in": ["Failed / Returned"]}, required=True), good_item("photo", item_type="photo")]
    created = env.client.post("/api/driver-overview/templates/delivery_checkout", headers=env.h["admin"], json={"name": "Real form", "items": items})
    assert created.status_code == 201, created.text
    new = created.json()
    assert new["version"] == 2 and new["active"] is True and new["is_draft"] is False and [i["key"] for i in new["items"]] == ["outcome", "reason", "photo"] and [i["position"] for i in new["items"]] == [1, 2, 3]
    after = env.client.get("/api/driver-overview/templates", headers=env.h["dispatcher"]).json()["delivery_checkout"]
    assert after["active"]["version"] == 2 and [(v["version"], v["active"]) for v in after["versions"]] == [(2, True), (1, False)]
    v1 = env.client.get("/api/driver-overview/templates/delivery_checkout/versions/1", headers=env.h["dispatcher"]).json()
    assert v1["version"] == 1 and v1["active"] is False and len(v1["items"]) == 14  # v1 kept, untouched
    with env.Session() as db:
        assert len(db.execute(select(FormTemplate).where(FormTemplate.kind == "delivery_checkout", FormTemplate.active.is_(True))).scalars().all()) == 1
        assert db.execute(select(FormTemplate).where(FormTemplate.kind == "acknowledge_checklist")).scalars().all()[0].active is True  # other kind untouched
    again = env.client.post("/api/driver-overview/templates/delivery_checkout", headers=env.h["admin"], json={"name": "Real form 2", "items": items}).json()
    assert again["version"] == 3
    assert env.client.get("/api/driver-overview/templates/delivery_checkout/versions/9", headers=env.h["admin"]).status_code == 404
    assert env.client.get("/api/driver-overview/templates/other_kind/versions/1", headers=env.h["admin"]).status_code == 404


def test_template_post_roles_and_validation(env):
    ok_items = [good_item("outcome", item_type="dropdown", options=["Delivered"])]
    assert env.client.post("/api/driver-overview/templates/delivery_checkout", headers=env.h["dispatcher"], json={"name": "x", "items": ok_items}).status_code == 403
    for who in ("driver", "officer"):
        assert env.client.get("/api/driver-overview/templates", headers=env.h[who]).status_code == 403
    assert env.client.post("/api/driver-overview/templates/delivery_checkout", json={"name": "x", "items": ok_items}).status_code == 401
    post = lambda kind, items, name="x": env.client.post(f"/api/driver-overview/templates/{kind}", headers=env.h["admin"], json={"name": name, "items": items})  # noqa: E731
    bad_cases = {
        "unknown type": ("delivery_checkout", ok_items + [good_item("a", item_type="telepathy")]),
        "checklist type on checkout": ("delivery_checkout", ok_items + [good_item("a", item_type="count")]),
        "checkout type on checklist": ("acknowledge_checklist", [good_item("a", item_type="textarea")]),
        "dropdown without options": ("delivery_checkout", [good_item("outcome", item_type="dropdown")]),
        "duplicate key": ("delivery_checkout", ok_items + [good_item("a"), good_item("a")]),
        "show_when forward reference": ("delivery_checkout", [ok_items[0], good_item("a", show_when={"key": "later", "in": ["x"]}), good_item("later")]),
        "show_when unknown key": ("delivery_checkout", ok_items + [good_item("a", show_when={"key": "ghost", "in": ["x"]})]),
        "bad key characters": ("delivery_checkout", ok_items + [good_item("has space")]),
        "no items": ("delivery_checkout", []),
        "no outcome on checkout": ("delivery_checkout", [good_item("a")]),
        "bad prefill": ("delivery_checkout", ok_items + [good_item("a", prefill="shoe_size")]),
    }
    for label, (kind, items) in bad_cases.items():
        response = post(kind, items)
        assert response.status_code == 422, (label, response.text)
        assert response.json()["detail"]["code"] == "validation" and response.json()["detail"]["errors"], label
    assert post("delivery_checkout", ok_items, name=" ").status_code == 422
    assert post("nonsense", ok_items).status_code == 404
    with env.Session() as db:  # every rejected attempt left the templates untouched
        assert [(x.kind, x.version, x.active) for x in db.execute(select(FormTemplate).order_by(FormTemplate.id)).scalars().all()] == [("acknowledge_checklist", 1, True), ("delivery_checkout", 1, True)]
        assert len(db.execute(select(FormTemplateItem)).scalars().all()) == 9 + 14


def test_overview_is_admin_and_dispatcher_only(env):
    seed(env)
    paths = ["/summary", "/board", "/drivers/9", "/trips/A", "/map", "/export.csv", "/templates"]
    for who in ("driver", "officer"):
        for path in paths:
            assert env.client.get(f"/api/driver-overview{path}", headers=env.h[who]).status_code == 403, (who, path)
    for path in paths:
        assert env.client.get(f"/api/driver-overview{path}").status_code == 401, path
        assert env.client.get(f"/api/driver-overview{path}", headers=env.h["dispatcher"]).status_code == 200, path


def test_results_are_cached_until_a_trip_write_bumps_the_generation(env):
    seed(env)
    reads: list[str] = []
    event.listen(env.engine, "before_cursor_execute", lambda conn, cur, stmt, *a: reads.append(stmt) if stmt.lstrip().upper().startswith("SELECT") and "trips" in stmt else None)
    env.client.get("/api/driver-overview/summary", headers=env.h["dispatcher"])
    first = len(reads)
    assert first > 0
    for _ in range(4):
        env.client.get("/api/driver-overview/summary", headers=env.h["admin"])
    assert len(reads) == first  # served from memory
    trip_events.bump()
    env.client.get("/api/driver-overview/summary", headers=env.h["dispatcher"])
    assert len(reads) > first
