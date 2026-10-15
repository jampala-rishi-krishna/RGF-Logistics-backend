"""Driver app simplification (API-CONTRACT sections 1-9): one-tap acknowledge, automatic handover at End trip, TO check-out,
dispatch unassign rules (preview / confirm / delivered), the order_unassigned audit event, dispatch_changes in the driver payload,
order_removed / trip_removed answers, and the Driver Overview timeline. SQLite + fake staff + in-memory assignment state; zero Zoho."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from models.sales_order_history import SalesOrderHistory
from models.trip import FormTemplate, FormTemplateItem, InvoiceHandover, InvoiceHandoverItem, SoAcknowledgement, TripEvent
from routers import assignment, driver_overview, officer
from services import driver_assignments, live_sales_order_cache
from tests import test_trip_workflow as base
from tests.test_trip_workflow import H, body, checkout_body, env, make_user, url  # noqa: F401  (env: the shared SQLite fixture)

BATCH = "b1"


@pytest.fixture()
def world(env, monkeypatch):
    app = env.client.app
    app.include_router(assignment.router)
    app.include_router(driver_overview.router)
    app.include_router(officer.router)
    orders: dict[str, SimpleNamespace] = {}

    def find_cached(order_id):
        return orders.get(order_id)

    def fake_sync(db, order):  # the Neon row follows the in-memory order (what sync_history_row does, minus Zoho-derived columns)
        row = db.get(SalesOrderHistory, order.id)
        for column in ("vehicle_id", "driver_id", "assignment_status", "assigned_at", "assigned_by", "manifest_id", "route_id"):
            setattr(row, column, getattr(order, column))

    monkeypatch.setattr(live_sales_order_cache, "find_cached", find_cached)
    monkeypatch.setattr(assignment, "sync_history_row", fake_sync)
    monkeypatch.setattr(assignment, "invalidate_fleet_cache", lambda: None)
    monkeypatch.setattr(live_sales_order_cache, "invalidate_assigned_zoho", lambda: None)
    monkeypatch.setattr(live_sales_order_cache, "invalidate_windows", lambda: None)
    monkeypatch.setattr(live_sales_order_cache, "_assignment_state", {})
    env.orders = orders
    env.dispatcher = make_user(env, "dispatcher", None, "Dina Dispatch")
    return env


def add(env, so_id, *, number=None, batch=BATCH, offset=0, order_type="SO", helpers=None):
    day = driver_assignments.manila_today()
    number = number or f"SO-{so_id}"
    with env.Session() as db:
        db.add(SalesOrderHistory(
            id=so_id, salesorder_number=number, customer_name=f"Cust {so_id}", expected_shipment_date=day, synced_at=datetime.now(timezone.utc),
            shipping_address={"address": f"{so_id} St", "city": "Makati"}, vehicle_id="NAA1", driver_id=9, helper_ids=helpers or [9],
            assigned_at=datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc) + timedelta(minutes=offset), assignment_status="assigned", assignment_batch_id=batch, order_type=order_type))
        db.commit()
    env.orders[so_id] = SimpleNamespace(id=so_id, salesorder_number=number, assignment_status="assigned", raw_json={}, manifest_id=None, route_id=None, vehicle_id="NAA1", driver_id=9,
                                        helper_ids=helpers or [9], assigned_at=None, assigned_by=None, expected_shipment_date=day, order_type=order_type)
    live_sales_order_cache.set_assignment(so_id, assignment_status="assigned", assignment_batch_id=batch, driver_id=9, helper_ids=helpers or [9], expected_shipment_date=day)


def count(env, model, **where):
    with env.Session() as db:
        query = select(func.count()).select_from(model)
        for key, value in where.items():
            query = query.where(getattr(model, key) == value)
        return db.scalar(query)


def ack_all(env, h, batch=BATCH, **extra):
    return env.client.post(url("acknowledge-all", batch), headers=h, json=body(**extra))


def to_stop(env, h, so, outcome="Delivered", **answers):
    assert env.client.post(url(f"orders/{so}/check-in"), headers=h, json=body()).status_code == 200
    r = env.client.post(url(f"orders/{so}/check-out"), headers=h, json=checkout_body(outcome, **answers))
    assert r.status_code == 200, r.text
    return r


def dispatch(env):
    return H(env.dispatcher, "dispatcher")


# ---- 1. one-tap acknowledge ----------------------------------------------------------------------------------------------


def test_one_tap_acknowledges_every_order_in_one_transaction_and_replays(world):
    env = world
    add(env, "s1")
    add(env, "s2", offset=1)
    add(env, "TO:5", number="TO-00005", offset=2, order_type="TO")
    h = H(make_user(env, "driver", 9, "Albert"))
    assert base.ack(env, h, "s1").status_code == 200  # the single endpoint keeps working
    payload = body(gps={"lat": 14.5, "lng": 121.0, "accuracy_m": 6})
    r = env.client.post(url("acknowledge-all"), headers=h, json=payload)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and sorted(out["acknowledged"]) == ["TO:5", "s2"] and out["already_acknowledged"] == ["s1"] and out["replayed"] is False
    assert out["batch"]["trip"]["chip"] == "ready"
    with env.Session() as db:
        acks = db.execute(select(SoAcknowledgement)).scalars().all()
        assert sorted(a.so_id for a in acks) == ["TO:5", "s1", "s2"]
        bulk = [a for a in acks if a.so_id != "s1"]
        assert all(a.gps["lat"] == 14.5 and a.answers == [] and a.by_role == "driver" for a in bulk)
    assert count(env, TripEvent, event_type="acknowledged") == 3
    again = env.client.post(url("acknowledge-all"), headers=h, json=payload).json()
    assert again["replayed"] is True and sorted(again["acknowledged"]) == ["TO:5", "s2"]
    assert count(env, SoAcknowledgement) == 3 and count(env, TripEvent) == 3
    nothing_left = ack_all(env, h).json()
    assert nothing_left["acknowledged"] == [] and len(nothing_left["already_acknowledged"]) == 3 and count(env, TripEvent) == 3
    assert env.zoho_calls == []


def test_checklist_off_by_default_and_enforced_once_a_real_template_is_saved(world):
    env = world
    add(env, "s1")
    h = H(make_user(env))
    today = env.client.get("/api/driver/today", headers=h).json()
    assert today["acknowledge_checklist_enabled"] is False and today["photo_storage_enabled"] is False
    assert env.client.get("/api/driver/templates", headers=h).json()["acknowledge_checklist_enabled"] is False
    with env.Session() as db:
        db.get(FormTemplate, 1).is_draft = False
        db.commit()
    driver_assignments.reset_cache()
    assert env.client.get("/api/driver/assignments/b1", headers=h).json()["acknowledge_checklist_enabled"] is True
    missing = ack_all(env, h)
    assert missing.status_code == 422 and missing.json()["detail"]["code"] == "validation" and all(e["key"].startswith("s1:") for e in missing.json()["detail"]["errors"])
    assert count(env, SoAcknowledgement) == 0
    ok = ack_all(env, h, answers={"s1": base.ack_answers(env)})
    assert ok.status_code == 200 and ok.json()["acknowledged"] == ["s1"]


# ---- 2 + 5. TO check-out and the automatic handover --------------------------------------------------------------------


def _with_invoice_item(env):
    with env.Session() as db:
        db.add(FormTemplateItem(template_id=2, position=0, key="invoice_dr_number", label="Invoice / DR number", item_type="numeric_text", required=True, active=True))
        db.commit()


def test_to_checkout_uses_the_to_number_and_end_trip_creates_the_handover(world):
    env = world
    _with_invoice_item(env)
    add(env, "s1")
    add(env, "TO:5", number="TO-00005", offset=1, order_type="TO")
    h = H(make_user(env, "driver", 9, "Albert"))
    detail = env.client.get("/api/driver/assignments/b1", headers=h).json()
    by_id = {o["id"]: o for o in detail["orders"]}
    assert by_id["TO:5"]["order_type"] == "TO" and by_id["TO:5"]["invoice_no_prefill"] == "TO-00005" and by_id["s1"]["invoice_no_prefill"] is None
    assert detail["dispatch_changes"] == []
    assert ack_all(env, h).status_code == 200
    assert env.client.post(url("start"), headers=h, json=body()).status_code == 200
    # an SO still needs its invoice number
    env.client.post(url("orders/s1/check-in"), headers=h, json=body())
    no_invoice = env.client.post(url("orders/s1/check-out"), headers=h, json=checkout_body())
    assert no_invoice.status_code == 422 and [e["key"] for e in no_invoice.json()["detail"]["errors"]] == ["invoice_dr_number"]
    assert env.client.post(url("orders/s1/check-out"), headers=h, json=checkout_body(invoice_dr_number="000777")).status_code == 200
    # a TO needs none, and a number the phone sends is ignored: the server stores the TO#
    to = to_stop(env, h, "TO:5", invoice_dr_number="999")
    stop = next(o for o in to.json()["batch"]["orders"] if o["id"] == "TO:5")["workflow"]["stop"]
    assert stop["invoice_no"] == "TO-00005"
    ended = env.client.post(url("end"), headers=h, json=body()).json()
    handover = ended["handover"]
    assert handover["status"] == "pending" and ended["batch"]["trip"]["status"] == "handover_submitted"
    assert sorted((i["so_id"], i["order_type"], i["invoice_no"], i["status"]) for i in handover["items"]) == [("TO:5", "TO", "TO-00005", "pending"), ("s1", "SO", "000777", "pending")]
    with env.Session() as db:
        row = db.execute(select(InvoiceHandover)).scalar_one()
        assert row.officer_user_id is None and row.submitted_by_user_id is not None
    assert count(env, InvoiceHandoverItem) == 2 and count(env, TripEvent, event_type="handover_submitted") == 1
    # replaying End returns the same handover; the old handover endpoint never makes a second one
    assert env.client.post(url("end"), headers=h, json=body()).json()["handover"]["id"] == handover["id"]
    assert env.client.post(url("handover"), headers=h, json=body()).json()["handover"]["id"] == handover["id"]
    assert count(env, InvoiceHandover) == 1
    # the officer queue shows it like any handover
    off = H(make_user(env, "logistics_officer", None, "Olivia"), "logistics_officer")
    queue = env.client.get("/api/officer/handovers", headers=off).json()
    assert [q["handover_id"] for q in queue] == [handover["id"]] and queue[0]["officer"] is None and queue[0]["totals"]["invoices"] == 2
    # Driver's already-cached Today must change after an officer validates.
    before = env.client.get("/api/driver/today", headers=h).json()["batches"][0]
    assert before["step"]["step"] == 3
    assert {i["officer_status"] for i in before["invoice_handover_items"]} == {"pending"}
    with env.Session() as db:
        invoice_items = db.execute(select(InvoiceHandoverItem).order_by(InvoiceHandoverItem.id)).scalars().all()
        validations = [{"item_id": i.id, "status": "missing" if i.so_id == "s1" else "received", "note": "Not returned" if i.so_id == "s1" else None} for i in invoice_items]
    result = env.client.post(f"/api/officer/handovers/{handover['id']}/validate", headers=off, json={**body(), "items": validations})
    assert result.status_code == 200, result.text
    after = env.client.get("/api/driver/today", headers=h).json()["batches"][0]
    assert {i["so_id"]: i["officer_status"] for i in after["invoice_handover_items"]} == {"s1": "missing", "TO:5": "received"}
    assert after["step"]["sub"]["missing"] == 1


# ---- 3, 4, 6, 7, 8, 9. unassign ------------------------------------------------------------------------------------------


def test_unassign_removes_the_order_from_the_driver_payload_at_once(world):
    env = world
    add(env, "s1")
    add(env, "s2", offset=1)
    h = H(make_user(env))
    assert [o["id"] for o in env.client.get("/api/driver/assignments/b1", headers=h).json()["orders"]] == ["s1", "s2"]
    assert env.client.get("/api/driver/today", headers=h).json()["summary"]["assigned"] == 2  # now cached in memory
    r = env.client.post("/api/load-planning/assignments/s2/unassign", headers=dispatch(env))
    assert r.status_code == 200, r.text  # plain "assigned": no confirmation needed
    today = env.client.get("/api/driver/today", headers=h).json()
    assert today["summary"]["assigned"] == 1 and [c["kind"] for c in today["dispatch_changes"]] == ["order_removed"]
    assert today["dispatch_changes"][0]["so_id"] == "s2" and today["dispatch_changes"][0]["so_number"] == "SO-s2" and today["dispatch_changes"][0]["at"]
    assert [o["id"] for o in env.client.get("/api/driver/assignments/b1", headers=h).json()["orders"]] == ["s1"]
    removed = base.ack(env, h, "s2")
    assert removed.status_code == 409 and removed.json()["detail"] == {"code": "order_removed", "message": "This order was removed by dispatch"}
    with env.Session() as db:
        event = db.execute(select(TripEvent).where(TripEvent.event_type == "order_unassigned")).scalar_one()
        assert (event.batch_id, event.so_id, event.user_id, event.role, event.gps, event.idempotency_key) == ("b1", "s2", env.dispatcher, "dispatcher", None, None)
        assert event.result["previous_state"] == "assigned" and event.result["by_name"] == "Dina Dispatch" and event.result["order_type"] == "SO"


def test_acknowledged_order_needs_confirm_then_is_audited(world):
    env = world
    add(env, "s1")
    add(env, "s2", offset=1)
    h = H(make_user(env, "driver", 9, "Albert Atienza"))
    assert base.ack(env, h, "s1").status_code == 200
    preview = env.client.post("/api/load-planning/assignments/unassign-preview", headers=dispatch(env), json={"order_ids": ["s1", "s2"]}).json()
    rows = {o["id"]: o for o in preview["orders"]}
    assert preview["needs_confirm"] is True
    assert rows["s1"]["state"] == "acknowledged" and rows["s1"]["needs_confirm"] and rows["s1"]["acknowledged_by"] == "Albert Atienza" and rows["s1"]["acknowledged_at"]
    assert rows["s1"]["message"].startswith("SO-s1: acknowledged by Albert Atienza at ") and rows["s2"]["state"] == "assigned" and not rows["s2"]["needs_confirm"]
    refused = env.client.post("/api/load-planning/assignments/s1/unassign", headers=dispatch(env))
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "confirm_required" and refused.json()["detail"]["orders"][0]["id"] == "s1"
    bulk_refused = env.client.post("/api/load-planning/assignments/bulk-unassign", headers=dispatch(env), json={"order_ids": ["s1", "s2"]})
    assert bulk_refused.status_code == 409 and bulk_refused.json()["detail"]["code"] == "confirm_required"
    assert count(env, TripEvent, event_type="order_unassigned") == 0 and live_sales_order_cache.get_assignment("s1")["assignment_status"] == "assigned"  # nothing done
    ok = env.client.post("/api/load-planning/assignments/s1/unassign", headers=dispatch(env), json={"confirm": True})
    assert ok.status_code == 200
    with env.Session() as db:
        event = db.execute(select(TripEvent).where(TripEvent.event_type == "order_unassigned")).scalar_one()
        assert event.result["previous_state"] == "acknowledged" and event.result["acknowledged_by"] == "Albert Atienza" and event.result["acknowledged_at"]
    # the Driver Overview timeline still shows it after the order left the batch
    timeline = env.client.get("/api/driver-overview/trips/b1", headers=dispatch(env)).json()
    entry = next(e for e in timeline["events"] if e["type"] == "order_unassigned")
    assert (entry["so_id"], entry["so_number"], entry["order_type"], entry["previous_state"], entry["actor_name"]) == ("s1", "SO-s1", "SO", "acknowledged", "Dina Dispatch") and entry["at"]
    assert next(o for o in timeline["orders"] if o["so_id"] == "s1")["removed"] is True
    # the rest of the trip is not blocked by the removed order
    assert ack_all(env, h).json()["acknowledged"] == ["s2"]
    assert env.client.post(url("start"), headers=h, json=body()).status_code == 200


def test_started_checked_in_and_delivered_orders(world):
    env = world
    _with_invoice_item(env)
    add(env, "s1")
    add(env, "s2", offset=1)
    add(env, "s3", offset=2)
    h = H(make_user(env))
    ack_all(env, h)
    env.client.post(url("start"), headers=h, json=body())
    to_stop(env, h, "s1", invoice_dr_number="1")
    env.client.post(url("orders/s2/check-in"), headers=h, json=body())
    rows = {o["id"]: o for o in env.client.post("/api/load-planning/assignments/unassign-preview", headers=dispatch(env), json={"order_ids": ["s1", "s2", "s3"]}).json()["orders"]}
    assert rows["s1"]["state"] == "delivered" and rows["s1"]["blocked"] and rows["s1"]["reason"] and not rows["s1"]["needs_confirm"]
    assert rows["s2"]["state"] == "checked_in" and rows["s2"]["needs_confirm"] and rows["s2"]["checked_in_at"] and rows["s2"]["trip_started_at"]
    assert rows["s3"]["state"] == "trip_started" and rows["s3"]["needs_confirm"] and "trip started" in rows["s3"]["message"]
    delivered = env.client.post("/api/load-planning/assignments/s1/unassign", headers=dispatch(env), json={"confirm": True})
    assert delivered.status_code == 409 and delivered.json()["detail"]["code"] == "delivered" and delivered.json()["detail"]["message"]
    bulk = env.client.post("/api/load-planning/assignments/bulk-unassign", headers=dispatch(env), json={"order_ids": ["s1", "s2"], "confirm": True}).json()
    assert bulk["unassigned"] == ["s2"] and [s["id"] for s in bulk["skipped"]] == ["s1"]
    with env.Session() as db:
        assert db.execute(select(TripEvent.result).where(TripEvent.event_type == "order_unassigned")).scalar_one()["previous_state"] == "checked_in"
    # the checked-in stop of the removed order no longer blocks the next check-in, nor End
    assert env.client.post(url("orders/s2/check-out"), headers=h, json=checkout_body(invoice_dr_number="2")).json()["detail"]["code"] == "order_removed"
    to_stop(env, h, "s3", invoice_dr_number="3")
    ended = env.client.post(url("end"), headers=h, json=body())
    assert ended.status_code == 200 and sorted(i["so_id"] for i in ended.json()["handover"]["items"]) == ["s1", "s3"]


def test_whole_trip_removed(world):
    env = world
    add(env, "s1", helpers=[9, 17])
    helper = H(make_user(env, "helper", 17), "helper")
    stranger = H(make_user(env, "driver", 10), "driver")
    assert env.client.get("/api/driver/today", headers=helper).json()["summary"]["assigned"] == 1
    assert env.client.post("/api/load-planning/assignments/bulk-unassign", headers=dispatch(env), json={"order_ids": ["s1"]}).json()["unassigned"] == ["s1"]
    today = env.client.get("/api/driver/today", headers=helper).json()
    assert today["batches"] == [] and today["dispatch_changes"] == [{"kind": "trip_removed", "batch_id": "b1", "so_id": None, "so_number": None, "at": today["dispatch_changes"][0]["at"]}]
    r = env.client.post(url("acknowledge-all"), headers=helper, json=body())
    assert r.status_code == 409 and r.json()["detail"] == {"code": "trip_removed", "message": "Trip removed by dispatch"}
    assert env.client.post(url("start"), headers=stranger, json=body()).status_code == 404  # never on it: same answer as before
    assert env.client.get("/api/driver/today", headers=stranger).json()["dispatch_changes"] == []
    timeline = env.client.get("/api/driver-overview/trips/b1", headers=dispatch(env)).json()
    assert [e["type"] for e in timeline["events"]] == ["order_unassigned"] and timeline["orders"] == []
    assert env.zoho_calls == []
