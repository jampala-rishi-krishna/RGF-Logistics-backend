import uuid

import pytest
from sqlalchemy import select

from auth.dependencies import CurrentUser
from models.trip import InvoiceHandoverItem, Trip, TripEvent, TripStop
from routers import driver
from services import order_tracker, order_tracker_facts
from tests import trip_test_support as t
from tests.test_overview import env, seed


@pytest.mark.parametrize("role", ["planner", "dispatcher", "admin"])
def test_receipt_refreshes_overview_mobile_and_tracker_without_changing_outcome(env, monkeypatch, role):
    day, _ = seed(env)
    uid = t.make_user(env, name="Invoice Receiver", email=f"{role}@example.test", role=role)
    headers = t.bearer(uid, role)
    env.app.include_router(driver.router)
    signals = []
    from services.ws_manager import manager
    async def broadcast(value): signals.append(value)
    monkeypatch.setattr(manager, "broadcast", broadcast)
    before = env.client.get("/api/driver-overview/trips/B", headers=headers).json()
    items = before["invoices"]
    assert all(i["status"] == "with_officer" and i["item_id"] for i in items)
    order_tracker.history_cache.put("test-receipt", {"stale": True})
    for item in items:
        body = {"idempotency_key": str(uuid.uuid4()), "status": "received"}
        url = f"/api/driver-overview/invoices/{item['item_id']}/receive"
        response = env.client.post(url, headers=headers, json=body)
        assert response.status_code == 200, response.text
        assert env.client.post(url, headers=headers, json=body).json()["replayed"]
    assert order_tracker.history_cache.get("test-receipt") is None
    assert signals and all(s == {"type": "TRIP_STATE_CHANGED"} for s in signals)
    after = env.client.get("/api/driver-overview/trips/B", headers=headers).json()
    assert after["trip"]["status"] == "closed"
    assert all(i["status"] == "received" for i in after["invoices"])
    assert [o["stop"]["checkout"]["outcome"] for o in after["orders"]] == [o["stop"]["checkout"]["outcome"] for o in before["orders"]]
    assert not [r for r in env.client.get("/api/driver-overview/hardcopies", headers=headers).json()["rows"] if r["batch_id"] == "B"]
    mobile = env.client.get("/api/driver/assignments/B?driver_id=10", headers=env.h["admin"])
    assert mobile.status_code == 200, mobile.text
    assert mobile.json()["trip"]["status"] == "closed"
    assert all(i["officer_status"] == "received" for i in mobile.json()["invoice_handover_items"])
    from models.sales_order_history import SalesOrderHistory
    with env.Session() as db:
        rows = list(db.execute(select(SalesOrderHistory).where(SalesOrderHistory.assignment_batch_id == "B")).scalars())
        monkeypatch.setattr(order_tracker.live, "cached_assigned_records", lambda: rows)
        monkeypatch.setattr(order_tracker.live, "get_assignment", lambda _: {})
        monkeypatch.setattr(order_tracker.live, "cached_detail", lambda _: None)
        order_tracker_facts.facts.clear()
        card = order_tracker.get_order(CurrentUser(uid, "receiver@example.test", "admin", "Receiver", "active"), "SO-B1", day, day, db)
        assert card["status"] == "Delivered"
        assert card["sections"]["proof"]["value"]["officer_invoice"]["status"] == "received"
        assert set(db.execute(select(TripEvent.role).where(TripEvent.batch_id == "B", TripEvent.event_type == "invoice_resolved")).scalars()) == {role}


def test_receipt_rejects_unhanded_invoice_and_other_roles(env):
    seed(env)
    with env.Session() as db:
        item = db.execute(select(InvoiceHandoverItem).where(InvoiceHandoverItem.so_id == "b1")).scalar_one()
        item.handed_over = False
        item_id = item.id
        db.commit()
    url = f"/api/driver-overview/invoices/{item_id}/receive"
    for role in ["driver", "helper", "warehouse", "sales", "wet_market", "logistics_officer"]:
        uid = t.make_user(env, name=role, email=f"{role}@example.test", role=role)
        assert env.client.post(url, headers=t.bearer(uid, role), json={"status": "received"}).status_code == 403
    assert env.client.post(url, headers=env.h["dispatcher"], json={"status": "received"}).status_code == 409
    with env.Session() as db:
        assert db.get(InvoiceHandoverItem, item_id).officer_status == "pending"


def test_receipt_closes_legacy_returned_trip(env):
    seed(env)
    with env.Session() as db:
        item_id = db.execute(select(InvoiceHandoverItem.id).where(InvoiceHandoverItem.so_id == "e1")).scalar_one()
    response = env.client.post(f"/api/driver-overview/invoices/{item_id}/receive", headers=env.h["dispatcher"], json={"idempotency_key": str(uuid.uuid4()), "status": "received"})
    assert response.status_code == 200, response.text
    with env.Session() as db:
        assert db.execute(select(Trip).where(Trip.batch_id == "E")).scalar_one().status == "closed"


def test_received_invoice_moves_to_collected_with_receiver_time_and_delivery_result(env):
    day, yesterday = seed(env)
    headers = env.h["dispatcher"]
    initial = env.client.get("/api/driver-overview/trips/B", headers=headers).json()["invoices"]
    for invoice in initial[:2]:
        response = env.client.post(f"/api/driver-overview/invoices/{invoice['item_id']}/receive", headers=headers, json={"idempotency_key": str(uuid.uuid4()), "status": "received"})
        assert response.status_code == 200, response.text
    pending = env.client.get("/api/driver-overview/hardcopies", headers=headers).json()
    assert not {"SO-B1", "SO-B2"} & {r["so_number"] for r in pending["rows"]}

    response = env.client.get("/api/driver-overview/hardcopies?collected=true", headers=headers)
    assert response.status_code == 200, response.text
    collected = response.json()
    assert collected["total"] == 2 and collected["counts"] == {"received": 2}
    assert {r["delivery_outcome"] for r in collected["rows"]} == {"Delivered", "Partially delivered"}
    assert all(r["state_label"] == "Collected" and r["received_by"] == "Dee Dispatcher" and r["since"] and r["delivery_date"] == str(day) for r in collected["rows"])
    for query in ["q=so b0002", "q=jaymar", "warehouse=METS", f"date={day}"]:
        result = env.client.get("/api/driver-overview/hardcopies?collected=true&" + query, headers=headers).json()
        assert result["total"] > 0, query
    assert env.client.get(f"/api/driver-overview/hardcopies?collected=true&date={yesterday}", headers=headers).json()["total"] == 0
    # A received item remains authoritative even if an old trip still says on_trip.
    with env.Session() as db:
        db.execute(Trip.__table__.update().where(Trip.batch_id == "B").values(status="on_trip"))
        db.commit()
    from services import trip_events
    trip_events.bump()
    pending = env.client.get("/api/driver-overview/hardcopies", headers=headers).json()
    assert not {"SO-B1", "SO-B2"} & {r["so_number"] for r in pending["rows"]}


def test_collected_calendar_filters_delivery_day_and_inclusive_range(env):
    from datetime import timedelta
    day, yesterday = seed(env)
    headers = env.h["dispatcher"]
    with env.Session() as db:
        ids = list(db.execute(select(InvoiceHandoverItem.id).where(InvoiceHandoverItem.so_id.in_(("b1", "e1")))).scalars())
    for item_id in ids:
        response = env.client.post(f"/api/driver-overview/invoices/{item_id}/receive", headers=headers, json={"status": "received", "idempotency_key": str(uuid.uuid4())})
        assert response.status_code == 200, response.text

    def collected(**scope):
        response = env.client.get("/api/driver-overview/hardcopies", params={"collected": "true", **scope}, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    for selected, expected in [(day, {"SO-B1"}), (yesterday, {"SO-E1"}), (day + timedelta(days=1), set())]:
        result = collected(date=str(selected))
        assert {row["so_number"] for row in result["rows"]} == expected
        assert result["total"] == len(expected)
        assert result["counts"] == ({"received": 1} if expected else {})
    result = collected(**{"from": str(yesterday), "to": str(day)})
    assert {row["so_number"] for row in result["rows"]} == {"SO-B1", "SO-E1"}
    assert result["total"] == 2 and result["counts"] == {"received": 2}
    for scope in [{"from": str(day)}, {"from": str(day), "to": str(yesterday)}]:
        assert env.client.get("/api/driver-overview/hardcopies", params={"collected": "true", **scope}, headers=headers).status_code == 422
