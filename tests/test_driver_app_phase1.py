"""Driver app rebuild, backend side: the Delivery time log template, Invoice/DR numbers on the officer's checklist, issues, alerts,
paginated history, the Today summary and the profile. In-memory SQLite, a fake staff directory, and a guard that fails on any Zoho call."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from models.trip import DriverAlertRead, DriverIssue, InvoiceHandoverItem
from models.sales_order_lines import SalesOrderLine
from routers import driver as driver_router, driver_extra, driver_overview, driver_trip, officer
from services import overview
from tests import trip_test_support as t

TIME_LOG_ITEMS = [
    {"key": "invoice_dr_number", "label": "Invoice or DR Number", "item_type": "numeric_text", "required": True},
    {"key": "outcome", "label": "Delivery outcome", "item_type": "radio", "required": True, "options": ["Delivered", "Partially delivered", "Failed"], "app_only": True},
    {"key": "reason", "label": "Reason", "item_type": "textarea", "required": True, "show_when": {"key": "outcome", "in": ["Partially delivered", "Failed"]}, "app_only": True},
    {"key": "delivery_photo", "label": "Photo (optional)", "item_type": "photo", "required": False, "app_only": True},
    {"key": "notes", "label": "Notes (optional)", "item_type": "textarea", "required": False, "app_only": True},
]


def key() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def env(monkeypatch):
    env = t.build_env(monkeypatch, driver_router.router, driver_trip.router, driver_extra.router, driver_overview.router, officer.router)
    t.seed_templates(env)
    env.admin = t.make_user(env, name="Ada Admin", email="ada@example.test", role="admin")
    env.dispatcher = t.make_user(env, name="Dee Dispatcher", email="dee@example.test", role="dispatcher")
    env.driver = t.make_user(env, name="Emert", email="emert@example.test", role="driver", staff_id=9)
    env.helper = t.make_user(env, name="Raymond", email="ray@example.test", role="helper", staff_id=17)
    env.other = t.make_user(env, name="Paul", email="paul@example.test", role="driver", staff_id=10)
    env.officer = t.make_user(env, name="Olivia Officer", email="olivia@example.test", role="logistics_officer")
    env.h = {n: t.bearer(getattr(env, n), role) for n, role in (("admin", "admin"), ("dispatcher", "dispatcher"), ("driver", "driver"), ("helper", "helper"), ("other", "driver"), ("officer", "logistics_officer"))}
    with env.Session() as db:
        overview.create_template_version(db, "delivery_checkout", env.admin, "Delivery time log", TIME_LOG_ITEMS)
    yield env
    assert env.zoho_calls == []
    env.engine.dispose()


def stamp(**extra):
    return {"idempotency_key": key(), "client_ts": datetime.now(timezone.utc).isoformat(), "gps": {"lat": 14.5, "lng": 121.0, "accuracy_m": 10}, **extra}


def seed_started_trip(env, batch="B1", sos=("s1", "s2"), day=None):
    day = day or t.today()
    for i, so in enumerate(sos):
        t.add_so(env, so, number=f"SO-{so}", customer=f"Customer {so}", batch=batch, driver=9, helpers=(17,), day=day, vehicle="DCD8954", offset=i)
    trip = t.add_trip(env, batch, status="on_trip", day=day, driver=9, helpers=(17,), vehicle="DCD8954", started=t.utc(day, 8))
    for i, so in enumerate(sos, 1):
        t.add_ack(env, trip, batch, so, at=t.utc(day, 7, i), user_id=env.driver)
        t.add_stop(env, trip, so, i, number=f"SO-{so}")
    return trip


def post(env, who, path, body):
    return env.client.post(path, headers=env.h[who], json=body)


# ---- the Delivery time log ----------------------------------------------------------------------------------------------------


def test_the_active_checkout_template_is_the_delivery_time_log_with_app_only_flags(env):
    template = env.client.get("/api/driver/templates", headers=env.h["driver"]).json()["delivery_checkout"]
    assert template["name"] == "Delivery time log" and template["is_draft"] is False and template["version"] == 2
    by_key = {i["key"]: i for i in template["items"]}
    assert by_key["invoice_dr_number"]["app_only"] is False and by_key["invoice_dr_number"]["required"] is True
    assert all(by_key[k]["app_only"] for k in ("outcome", "reason", "delivery_photo", "notes"))
    settings = env.client.get("/api/driver-overview/templates", headers=env.h["dispatcher"]).json()["delivery_checkout"]
    assert settings["active"]["name"] == "Delivery time log" and [v["version"] for v in settings["versions"]] == [2, 1]  # the old DRAFT stays readable


def test_arrive_then_depart_records_invoice_dr_number_outcome_and_time_at_site(env):
    seed_started_trip(env)
    arrived = post(env, "driver", "/api/driver/batches/B1/orders/s1/check-in", stamp())
    assert arrived.status_code == 200, arrived.text
    stop = next(o for o in arrived.json()["batch"]["orders"] if o["id"] == "s1")["workflow"]["stop"]
    assert stop["status"] == "checked_in" and stop["checkin_at"]
    missing = post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", stamp(answers={"outcome": "Delivered"}))
    assert missing.status_code == 422 and {e["key"] for e in missing.json()["detail"]["errors"]} == {"invoice_dr_number"}
    letters = post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", stamp(answers={"invoice_dr_number": "12A", "outcome": "Delivered"}))
    assert letters.status_code == 422
    failed_no_reason = post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", stamp(answers={"invoice_dr_number": "123", "outcome": "Failed"}))
    assert failed_no_reason.status_code == 422 and failed_no_reason.json()["detail"]["errors"][0]["key"] == "reason"
    done = post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", stamp(answers={"invoice_dr_number": "000123", "outcome": "Delivered"}))
    assert done.status_code == 200, done.text
    stop = next(o for o in done.json()["batch"]["orders"] if o["id"] == "s1")["workflow"]["stop"]
    assert stop["outcome"] == "Delivered" and stop["status"] == "checked_out" and stop["invoice_no"] == "000123"  # leading zeros survive


def test_a_template_without_an_outcome_question_records_every_stop_as_delivered(env):
    items = [i for i in TIME_LOG_ITEMS if i["key"] in ("invoice_dr_number", "notes")]
    with env.Session() as db:
        overview.create_template_version(db, "delivery_checkout", env.admin, "Delivery time log (no outcome)", items)
    seed_started_trip(env)
    post(env, "driver", "/api/driver/batches/B1/orders/s1/check-in", stamp())
    done = post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", stamp(answers={"invoice_dr_number": "55"}))
    assert done.status_code == 200
    assert next(o for o in done.json()["batch"]["orders"] if o["id"] == "s1")["workflow"]["stop"]["outcome"] == "Delivered"


def test_officer_checklist_lines_carry_the_invoice_numbers_typed_at_check_out(env):
    seed_started_trip(env)
    for so, number in (("s1", "1001"), ("s2", "1002")):
        assert post(env, "driver", f"/api/driver/batches/B1/orders/{so}/check-in", stamp()).status_code == 200
        assert post(env, "driver", f"/api/driver/batches/B1/orders/{so}/check-out", stamp(answers={"invoice_dr_number": number, "outcome": "Delivered"})).status_code == 200
    assert post(env, "driver", "/api/driver/batches/B1/end", stamp()).status_code == 200
    handed = post(env, "driver", "/api/driver/batches/B1/handover", stamp(officer_user_id=env.officer, items=[{"so_id": "s1", "handed_over": True}, {"so_id": "s2", "handed_over": True}]))
    assert handed.status_code == 200, handed.text
    queue = env.client.get("/api/officer/handovers", headers=env.h["officer"]).json()
    detail = env.client.get(f"/api/officer/handovers/{queue[0]['handover_id']}", headers=env.h["officer"]).json()
    assert {(i["so_number"], i["invoice_no"]) for i in detail["items"]} == {("SO-s1", "1001"), ("SO-s2", "1002")}
    outstanding = env.client.get("/api/officer/outstanding", headers=env.h["officer"]).json()
    assert {(i["so_number"], i["invoice_no"]) for i in outstanding} == {("SO-s1", "1001"), ("SO-s2", "1002")}


# ---- issues -------------------------------------------------------------------------------------------------------------------


def issue(**over):
    return {"idempotency_key": key(), "category": "Damaged goods", "priority": "high", "description": "Two cartons crushed on the way", **over}


def test_a_driver_reports_an_issue_from_an_order_and_dispatch_sees_it(env):
    seed_started_trip(env)
    created = post(env, "driver", "/api/driver/issues", issue(batch_id="B1", so_id="s1"))
    assert created.status_code == 200, created.text
    body = created.json()["issue"]
    assert body["reference"].startswith("IS-") and body["status"] == "submitted" and body["so_number"] == "SO-s1" and body["vehicle"] == "DCD8954" and body["priority"] == "high"
    mine = env.client.get("/api/driver/issues", headers=env.h["driver"]).json()
    assert [i["reference"] for i in mine["issues"]] == [body["reference"]] and "Damaged goods" in mine["categories"]
    assert env.client.get("/api/driver/issues", headers=env.h["other"]).json()["issues"] == []  # never someone else's
    listing = env.client.get("/api/driver-overview/issues", headers=env.h["dispatcher"]).json()
    assert listing["issues"][0]["reference"] == body["reference"] and listing["issues"][0]["reporter"]["name"] == "Emert Hirondo"
    kpis = env.client.get("/api/driver-overview/summary", headers=env.h["dispatcher"]).json()["kpis"]
    assert kpis["issues_open"] == 1
    assert env.client.get("/api/driver/today", headers=env.h["driver"]).json()["summary"]["open_issues"] == 1


def test_issue_report_is_idempotent_validated_and_scoped(env):
    seed_started_trip(env)
    first = issue(batch_id="B1")
    a = post(env, "driver", "/api/driver/issues", first).json()
    b = post(env, "driver", "/api/driver/issues", first).json()
    assert b["replayed"] is True and a["issue"]["reference"] == b["issue"]["reference"]
    with env.Session() as db:
        assert db.execute(select(func.count()).select_from(DriverIssue)).scalar() == 1
    assert post(env, "driver", "/api/driver/issues", issue(category="Made up")).status_code == 422
    assert post(env, "driver", "/api/driver/issues", issue(description="no")).status_code == 422
    assert post(env, "driver", "/api/driver/issues", issue(priority="whenever")).status_code == 422
    assert post(env, "driver", "/api/driver/issues", issue(batch_id="B1", so_id="zzz")).status_code == 422
    assert post(env, "other", "/api/driver/issues", issue(batch_id="B1")).status_code == 404  # not their batch
    assert post(env, "driver", "/api/driver/issues", issue(photo_ids=[999])).status_code == 422
    profile_issue = post(env, "driver", "/api/driver/issues", issue(category="Application or technical issue"))
    assert profile_issue.status_code == 200 and profile_issue.json()["issue"]["batch_id"] is None  # from the profile: no trip reference
    for who in ("admin", "dispatcher", "officer"):
        assert post(env, who, "/api/driver/issues", issue()).status_code == 403


def test_dispatch_replies_and_the_driver_gets_an_alert(env):
    created = post(env, "driver", "/api/driver/issues", issue()).json()["issue"]
    assert env.client.get("/api/driver/alerts", headers=env.h["driver"]).json()["alerts"] == []
    reply = post(env, "dispatcher", f"/api/driver-overview/issues/{created['id']}/respond", {"status": "in_progress", "response": "Sending a replacement truck"})
    assert reply.status_code == 200 and reply.json()["changed"] is True
    again = post(env, "dispatcher", f"/api/driver-overview/issues/{created['id']}/respond", {"status": "in_progress", "response": "Sending a replacement truck"})
    assert again.json()["changed"] is False  # nothing written
    assert post(env, "driver", f"/api/driver-overview/issues/{created['id']}/respond", {"status": "closed"}).status_code == 403
    assert post(env, "dispatcher", f"/api/driver-overview/issues/{created['id']}/respond", {"status": "bogus"}).status_code == 422
    mine = env.client.get("/api/driver/issues", headers=env.h["driver"]).json()["issues"][0]
    assert mine["status"] == "in_progress" and mine["response"] == "Sending a replacement truck"
    alerts = env.client.get("/api/driver/alerts", headers=env.h["driver"]).json()
    assert alerts["unread"] == 1 and alerts["alerts"][0]["type"] == "issue" and alerts["alerts"][0]["link"] == "/driver/issues"
    assert env.client.get("/api/driver/alerts", headers=env.h["other"]).json()["alerts"] == []
    resolved = post(env, "dispatcher", f"/api/driver-overview/issues/{created['id']}/respond", {"status": "resolved"})
    assert resolved.json()["changed"] is True
    assert env.client.get("/api/driver-overview/summary", headers=env.h["dispatcher"]).json()["kpis"]["issues_open"] == 0


# ---- alerts -------------------------------------------------------------------------------------------------------------------


def test_alerts_new_assignment_changed_assignment_and_officer_validation(env):
    day = t.today()
    seed_started_trip(env, day=day)
    alerts = env.client.get("/api/driver/alerts", headers=env.h["driver"]).json()
    assert [a["type"] for a in alerts["alerts"]] == ["assigned"] and alerts["unread"] == 1
    assigned = alerts["alerts"][0]
    assert assigned["title"] == "New assignment" and assigned["link"] == "/driver/assignment/B1" and "DCD8954" in assigned["body"] and "2 orders" in assigned["body"]
    assert env.client.get("/api/driver/alerts", headers=env.h["helper"]).json()["unread"] == 1  # the helper is crew too
    assert env.client.get("/api/driver/alerts", headers=env.h["other"]).json()["alerts"] == []
    # the truck is swapped after the crew started from the original assignment
    with env.Session() as db:
        for row in db.execute(select(__import__("models.sales_order_history", fromlist=["SalesOrderHistory"]).SalesOrderHistory)).scalars().all():
            row.vehicle_id = "NEW9999"
        db.commit()
    types = {a["type"] for a in env.client.get("/api/driver/alerts", headers=env.h["driver"]).json()["alerts"]}
    assert types == {"assigned", "changed"}
    # the officer validates an invoice
    trip = 1
    handover_id, item_ids = t.add_handover(env, trip, "B1", [("s1", "SO-s1", "Customer s1", True, "received")], status="acknowledged", officer_user_id=env.officer)
    with env.Session() as db:
        item = db.get(InvoiceHandoverItem, item_ids[0])
        item.invoice_no, item.officer_checked_at = "7788", datetime.now(timezone.utc)
        db.commit()
    kinds = [a["type"] for a in env.client.get("/api/driver/alerts", headers=env.h["driver"]).json()["alerts"]]
    assert "validated" in kinds


def test_alerts_mark_read_is_idempotent_scoped_and_ignores_unknown_keys(env):
    seed_started_trip(env)
    first = env.client.get("/api/driver/alerts", headers=env.h["driver"]).json()
    alert_key = first["alerts"][0]["key"]
    assert post(env, "driver", "/api/driver/alerts/read", {"keys": [alert_key, "assigned:not-mine"]}).json() == {"ok": True, "marked": 1, "unread": 0}
    assert post(env, "driver", "/api/driver/alerts/read", {"keys": [alert_key]}).json()["marked"] == 0  # a repeat writes nothing
    with env.Session() as db:
        assert db.execute(select(func.count()).select_from(DriverAlertRead)).scalar() == 1
    assert env.client.get("/api/driver/alerts", headers=env.h["helper"]).json()["unread"] == 1  # the helper's own read state is separate
    assert post(env, "helper", "/api/driver/alerts/read", {"all": True}).json()["unread"] == 0
    assert post(env, "driver", "/api/driver/alerts/read", {}).status_code == 422
    for who in ("admin", "dispatcher", "officer"):
        assert env.client.get("/api/driver/alerts", headers=env.h[who]).status_code == 403


# ---- Today summary, profile, history ------------------------------------------------------------------------------------------


def test_today_summary_and_profile_fields(env):
    seed_started_trip(env)
    post(env, "driver", "/api/driver/batches/B1/orders/s1/check-in", stamp())
    post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", stamp(answers={"invoice_dr_number": "9", "outcome": "Failed", "reason": "closed"}))
    summary = env.client.get("/api/driver/today", headers=env.h["driver"]).json()["summary"]
    assert summary == {"assigned": 2, "completed": 1, "remaining": 1, "needs_attention": 1, "open_issues": 0, "trips": 1}
    me = env.client.get("/api/driver/me", headers=env.h["driver"]).json()
    assert me["staff_id"] == 9 and me["email"] == "emert@example.test" and me["vehicles_today"] == ["DCD8954"] and [p["name"] for p in me["crew_today"]] == ["Raymond Roblo"]
    assert env.client.get("/api/driver/me", headers=env.h["helper"]).json()["crew_today"][0]["name"] == "Emert Hirondo"


def seed_history(env):
    today = t.today()
    for days_ago, (batch, status, outcome) in enumerate([("H1", "closed", "Delivered"), ("H2", "closed", "Failed"), ("H3", "returned", "Delivered"), ("H4", "pending", None), ("H5", "closed", "Delivered")], 1):
        day = today - timedelta(days=days_ago)
        t.add_so(env, f"{batch}-a", number=f"SO-{batch}", batch=batch, driver=9, day=day, vehicle="DCD8954")
        if status == "pending":
            continue
        trip = t.add_trip(env, batch, status=status, day=day, driver=9, vehicle="DCD8954", started=t.utc(day, 8), ended=t.utc(day, 15))
        t.add_ack(env, trip, batch, f"{batch}-a", at=t.utc(day, 7), user_id=env.driver)
        t.add_stop(env, trip, f"{batch}-a", 1, number=f"SO-{batch}", status="checked_out", checkin=t.utc(day, 9), checkout=t.utc(day, 9, 20), outcome=outcome, user_id=env.driver)


def test_history_is_filtered_and_paginated_on_the_server(env):
    seed_history(env)
    page1 = env.client.get("/api/driver/history?days=30&page=1&page_size=2", headers=env.h["driver"]).json()
    assert page1["total"] == 5 and page1["has_more"] is True and [b["batch_id"] for b in page1["batches"]] == ["H1", "H2"]  # newest first
    page3 = env.client.get("/api/driver/history?days=30&page=3&page_size=2", headers=env.h["driver"]).json()
    assert [b["batch_id"] for b in page3["batches"]] == ["H5"] and page3["has_more"] is False
    completed = env.client.get("/api/driver/history?days=30&status=completed", headers=env.h["driver"]).json()
    assert completed["total"] == 4 and "H4" not in {b["batch_id"] for b in completed["batches"]}  # H4 never started
    failed = env.client.get("/api/driver/history?days=30&status=failed", headers=env.h["driver"]).json()
    assert [b["batch_id"] for b in failed["batches"]] == ["H2"] and failed["batches"][0]["progress"]["failed"] == 1
    ranged = env.client.get(f"/api/driver/history?from={(t.today() - timedelta(days=2)).isoformat()}&to={(t.today() - timedelta(days=1)).isoformat()}&status=all", headers=env.h["driver"]).json()
    assert ranged["total"] == 2
    assert env.client.get("/api/driver/history?days=30&status=weird", headers=env.h["driver"]).status_code == 422
    assert env.client.get("/api/driver/history?days=30&status=all", headers=env.h["other"]).json()["total"] == 0  # a driver only ever sees their own trips
    legacy = env.client.get("/api/driver/history?days=30", headers=env.h["driver"]).json()
    assert len(legacy["batches"]) == 5 and "page" not in legacy  # the old response shape is unchanged


def test_check_out_numeric_text_rejects_non_ascii_digits_and_overlong_values(env):
    seed_started_trip(env)
    post(env, "driver", "/api/driver/batches/B1/orders/s1/check-in", stamp())
    for bad in ("٣٤", "1" * 31, "12 34", "-5"):
        assert post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", stamp(answers={"invoice_dr_number": bad, "outcome": "Delivered"})).status_code == 422, bad


def test_invoice_hint_is_only_a_prefill_from_memory(env, monkeypatch):
    from types import SimpleNamespace

    from services import live_sales_order_cache

    t.add_so(env, "h1", number="SO-h1", batch="HB", driver=9, helpers=(), day=t.today(), vehicle="DCD8954")
    cached = SimpleNamespace(id="h1", raw_json={"line_items": [{"name": "x"}], "invoices": [{"invoice_number": "INV-000456"}]})
    monkeypatch.setattr(live_sales_order_cache, "find_cached", lambda order_id: cached if order_id == "h1" else None)
    monkeypatch.setattr(live_sales_order_cache, "cached_detail", lambda order_id: None)
    order = env.client.get("/api/driver/assignments/HB", headers=env.h["driver"]).json()["orders"][0]
    assert order["invoice_hint"] == "000456"
    cached.raw_json["invoices"].append({"invoice_number": "INV-000999"})
    assert env.client.get("/api/driver/assignments/HB", headers=env.h["driver"]).json()["orders"][0]["invoice_hint"] is None  # ambiguous: the crew types it


def test_lines_are_untouched_by_the_phase_one_changes(env):
    t.add_so(env, "x1", number="SO-x1", batch="XB", driver=9, day=t.today())
    with env.Session() as db:
        db.add(SalesOrderLine(sales_order_id="x1", name="n", quantity=2.0, unit="pck", weight_kg=None))
        db.commit()
    batch = env.client.get("/api/driver/today", headers=env.h["driver"]).json()["batches"][0]
    assert batch["total_packs"] == 2.0 and batch["kg_unknown_lines"] == 1 and batch["kg_complete"] is False


# ---- Driver Overview additions (Phase 2) --------------------------------------------------------------------------------------


def test_overview_live_trips_hardcopies_and_timeline_for_a_departed_stop(env, monkeypatch):
    from types import SimpleNamespace

    from services import live_gps_store

    seed_started_trip(env)
    assert post(env, "driver", "/api/driver/batches/B1/orders/s1/check-in", stamp()).status_code == 200
    assert post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", stamp(answers={"invoice_dr_number": "0042", "outcome": "Delivered"})).status_code == 200
    # memory-only live fix of the truck (the plate of the batch)
    monkeypatch.setattr(live_gps_store, "get", lambda plate: {"lat": 14.6, "lng": 121.0, "heading": 90, "speed_kph": 31, "ignition_on": True, "status": "Active", "gps_time": "2026-10-13T00:00:00+00:00", "last_updated": datetime.now(timezone.utc)} if plate == "DCD8954" else None)
    live = env.client.get("/api/driver-overview/live", headers=env.h["dispatcher"]).json()
    assert [(x["plate"], x["drivers"], x["fix"]["lat"]) for x in live["trucks"]] == [("DCD8954", ["Emert Hirondo"], 14.6)]
    trips = env.client.get("/api/driver-overview/trips", headers=env.h["dispatcher"]).json()
    assert trips["total"] == 1 and trips["trips"][0]["stops_done"] == 1 and trips["trips"][0]["delivered"] == 1 and trips["trips"][0]["state"] == "on_trip"
    copies = env.client.get("/api/driver-overview/hardcopies", headers=env.h["dispatcher"]).json()
    assert [(r["so_number"], r["invoice_no"], r["state"], r["holder"]) for r in copies["rows"]] == [("SO-s1", "0042", "with_driver", "Emert Hirondo")]
    timeline = env.client.get("/api/driver-overview/trips/B1", headers=env.h["dispatcher"]).json()
    first = next(o for o in timeline["orders"] if o["so_id"] == "s1")["stop"]
    assert first["checkout"]["invoice_no"] == "0042" and first["checkin"]["gps"]["lat"] == 14.5 and first["minutes_at_stop"] is not None
    board = env.client.get("/api/driver-overview/board", headers=env.h["dispatcher"]).json()["rows"]
    assert "SO-s1" in next(r for r in board if r["staff_id"] == 9)["so_numbers"]  # search by SO number
    csv_text = env.client.get("/api/driver-overview/export.csv", headers=env.h["dispatcher"]).content.decode("utf-8-sig")
    assert "invoice_dr_number" in csv_text.splitlines()[0] and ",0042," in csv_text
    for who in ("driver", "helper", "officer"):
        assert env.client.get("/api/driver-overview/hardcopies", headers=env.h[who]).status_code == 403
        assert env.client.get("/api/driver-overview/live", headers=env.h[who]).status_code == 403


def test_hardcopies_after_handover_are_with_the_officer_then_missing(env):
    seed_started_trip(env)
    for so, number in (("s1", "11"), ("s2", "12")):
        post(env, "driver", f"/api/driver/batches/B1/orders/{so}/check-in", stamp())
        post(env, "driver", f"/api/driver/batches/B1/orders/{so}/check-out", stamp(answers={"invoice_dr_number": number, "outcome": "Delivered"}))
    post(env, "driver", "/api/driver/batches/B1/end", stamp())
    post(env, "driver", "/api/driver/batches/B1/handover", stamp(officer_user_id=env.officer, items=[{"so_id": "s1", "handed_over": True}, {"so_id": "s2", "handed_over": False, "note": "customer kept it"}]))
    rows = {r["so_number"]: r for r in env.client.get("/api/driver-overview/hardcopies", headers=env.h["dispatcher"]).json()["rows"]}
    assert rows["SO-s1"]["state"] == "with_officer" and rows["SO-s1"]["holder"] == "Olivia Officer" and rows["SO-s1"]["invoice_no"] == "11"
    assert rows["SO-s2"]["state"] == "not_handed_over" and rows["SO-s2"]["holder"] == "Emert Hirondo"
