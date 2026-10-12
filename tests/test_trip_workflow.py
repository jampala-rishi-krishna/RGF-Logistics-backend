"""Crew trip workflow: state machine, idempotency, validation, crew scoping, route, caching. SQLite + fake staff; zero Zoho."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

os.environ.setdefault("JWT_SECRET", "test-secret-not-real")

import database  # noqa: E402
from auth import dependencies, security  # noqa: E402
from models.sales_order_history import SalesOrderHistory  # noqa: E402
from models.sales_order_lines import SalesOrderLine  # noqa: E402
from models.trip import (FormTemplate, FormTemplateItem, InvoiceHandover, InvoiceHandoverItem, SoAcknowledgement, Trip, TripEvent, TripPhoto, TripStop)  # noqa: E402
from models.user import User, UserInvite  # noqa: E402
from routers import driver as driver_router, driver_trip  # noqa: E402
from services import driver_assignments, live_sales_order_cache, media_storage, memory_tables, staff_directory_cache, trip_events, trip_workflow, user_cache  # noqa: E402
from tests.sqlite_helpers import make_engine, make_session_factory, seed_default_templates  # noqa: E402

STAFF = {
    9: {"id": 9, "name": "Emert", "title": "MC RIDER", "warehouse": "METS", "phone": "0948", "role": "DRIVER"},
    10: {"id": 10, "name": "Paul", "title": "MC RIDER", "warehouse": "METS", "phone": "0936", "role": "DRIVER"},
    1: {"id": 1, "name": "Helper One", "title": "DELIVERY HELPER", "warehouse": "METS", "phone": "01", "role": "HELPER"},
    17: {"id": 17, "name": "Raymond", "title": "DELIVERY HELPER", "warehouse": "METS", "phone": "017", "role": "HELPER"},
}
TABLES = (User, UserInvite, SalesOrderHistory, SalesOrderLine, FormTemplate, FormTemplateItem, Trip, SoAcknowledgement, TripStop, TripEvent, TripPhoto, InvoiceHandover, InvoiceHandoverItem)
TODAY = None


@pytest.fixture()
def env(monkeypatch):
    engine = make_engine(*(t.__table__ for t in TABLES))
    Session = make_session_factory(engine)
    monkeypatch.setattr(dependencies, "SessionLocal", Session)
    monkeypatch.setattr(staff_directory_cache, "get_by_id", lambda staff_id, **_: STAFF.get(staff_id))
    monkeypatch.setattr(live_sales_order_cache, "find_cached", lambda order_id: None)
    monkeypatch.setattr(media_storage, "is_enabled", lambda: False)
    user_cache._cache.clear()
    driver_assignments.reset_cache()
    zoho_calls: list[str] = []

    def no_zoho(*a, **k):
        zoho_calls.append("zoho")
        raise AssertionError("a trip endpoint tried to call Zoho")

    from services import zoho_client

    monkeypatch.setattr(zoho_client, "_request_http", no_zoho)
    monkeypatch.setattr(live_sales_order_cache, "fetch_sales_order_detail", no_zoho)
    monkeypatch.setattr(live_sales_order_cache, "ensure_zoho_data", no_zoho)
    app = FastAPI()
    app.include_router(driver_router.router)
    app.include_router(driver_trip.router)

    def get_test_db():
        with Session() as db:
            yield db

    app.dependency_overrides[database.get_db] = get_test_db
    with Session() as db:
        seed_default_templates(db)
    yield SimpleNamespace(client=TestClient(app), Session=Session, zoho_calls=zoho_calls, engine=engine, monkeypatch=monkeypatch)
    engine.dispose()


_counter = [0]


def make_user(env, role="driver", staff_id=9, name=None, status="active"):
    _counter[0] += 1
    with env.Session() as db:
        user = User(full_name=name or f"{role}{_counter[0]}", email=f"u{_counter[0]}@example.test", phone=639000000000 + _counter[0], role=role, status=status,
                    staff_id=staff_id, password_hash="x")
        db.add(user)
        db.commit()
        return user.id


def H(user_id, role="driver"):
    return {"Authorization": f"Bearer {security.create_access_token(user_id=user_id, role=role, email='x@example.test')}"}


def add_order(env, so_id, *, driver_id=9, helper_ids=None, batch="b1", offset=0, route_id=None, address=None, day=None):
    day = day or driver_assignments.manila_today()
    with env.Session() as db:
        db.add(SalesOrderHistory(
            id=so_id, salesorder_number=f"SO-{so_id}", customer_name=f"Cust {so_id}", expected_shipment_date=day, synced_at=datetime.now(timezone.utc),
            shipping_address=address or {"address": f"{so_id} St", "city": "Makati"}, vehicle_id="NAA1", driver_id=driver_id,
            helper_ids=helper_ids if helper_ids is not None else [driver_id], assigned_at=datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc) + timedelta(minutes=offset),
            assignment_status="assigned", assignment_batch_id=batch, route_id=route_id))
        db.commit()


def body(**extra):
    return {"idempotency_key": str(uuid.uuid4()), "client_ts": None, "gps": None, "gps_denied_reason": "denied", **extra}


def ack_answers(env):
    with env.Session() as db:
        items = db.execute(select(FormTemplateItem).where(FormTemplateItem.template_id == 1)).scalars().all()
    return [{"item_id": i.id, "checked": True} for i in items if i.item_type == "checkbox"]


def url(path, batch="b1"):
    return f"/api/driver/batches/{batch}/{path}"


def ack(env, h, so, batch="b1", **extra):
    return env.client.post(url(f"orders/{so}/acknowledge", batch), headers=h, json=body(answers=ack_answers(env), **extra))


def checkout_body(outcome="Delivered", **answers):
    base = {"outcome": outcome}
    if outcome in ("Delivered", "Partially delivered"):
        base["received_by"] = "Ana"
    if outcome != "Delivered":
        base["reason"] = "short"
    base.update(answers)
    return body(answers=base)


def start_trip(env, h, sos=("s1",), batch="b1"):
    for so in sos:
        assert ack(env, h, so, batch).status_code == 200
    r = env.client.post(url("start", batch), headers=h, json=body())
    assert r.status_code == 200, r.text
    return r


def counts(env):
    with env.Session() as db:
        return {m.__tablename__: db.scalar(select(func.count()).select_from(m)) for m in (Trip, SoAcknowledgement, TripStop, TripEvent, InvoiceHandover, InvoiceHandoverItem)}


def detail(env, h, batch="b1"):
    return env.client.get(f"/api/driver/assignments/{batch}", headers=h).json()


@pytest.fixture()
def crew(env):
    add_order(env, "s1")
    add_order(env, "s2", offset=5)
    return make_user(env, "driver", 9)


# ---- state machine ------------------------------------------------------------------------------------------------------


def test_full_trip_flow_and_chips(env, crew):
    h = H(crew)
    d = detail(env, h)
    assert d["trip"]["chip"] == "to_acknowledge" and d["trip"]["status"] == "pending" and d["trip"]["acknowledged"] == 0
    assert d["orders"][0]["workflow"]["can_acknowledge"] is True
    # today payload for a batch with no trip row
    today = env.client.get("/api/driver/today", headers=h).json()["batches"][0]
    assert today["trip"]["chip"] == "to_acknowledge" and today["trip"]["total"] == 2 and [c["role"] for c in today["crew"]] == ["driver"]
    r = ack(env, h, "s1")
    assert r.status_code == 200 and r.json()["batch"]["trip"]["acknowledged"] == 1 and r.json()["already_done"] is False
    ack(env, h, "s2")
    d = detail(env, h)
    assert d["trip"]["chip"] == "ready" and d["trip"]["can_start"] and d["trip"]["next_action"] == "start"
    r = env.client.post(url("start"), headers=h, json=body()).json()
    assert r["batch"]["trip"]["chip"] == "on_trip" and r["batch"]["trip"]["chip_label"] == "On trip · stop 1 of 2" and r["batch"]["stop_order_set"] is True
    assert env.client.post(url("orders/s1/check-in"), headers=h, json=body()).status_code == 200
    r = env.client.post(url("orders/s1/check-out"), headers=h, json=checkout_body()).json()
    assert r["batch"]["trip"]["stops_done"] == 1 and r["batch"]["trip"]["chip_label"] == "On trip · stop 2 of 2"
    env.client.post(url("orders/s2/check-in"), headers=h, json=body())
    env.client.post(url("orders/s2/check-out"), headers=h, json=checkout_body("Failed / Returned"))
    d = detail(env, h)
    assert d["trip"]["can_end"] and d["trip"]["next_action"] == "end"
    end = env.client.post(url("end"), headers=h, json=body()).json()
    assert end["batch"]["trip"]["chip"] == "returned" and end["batch"]["trip"]["can_submit_handover"]
    officer = make_user(env, "logistics_officer", None, "Off")
    assert env.client.get("/api/driver/officers", headers=h).json() == [{"id": officer, "name": "Off"}]
    items = [{"so_id": "s1", "handed_over": True}, {"so_id": "s2", "handed_over": False, "note": "customer kept it"}]
    r = env.client.post(url("handover"), headers=h, json=body(officer_user_id=officer, items=items)).json()
    assert r["batch"]["trip"]["chip"] == "handover_pending" and r["batch"]["trip"]["handover_status"] == "pending" and r["batch"]["trip"]["officer"]["id"] == officer
    c = counts(env)
    assert c["trips"] == 1 and c["so_acknowledgements"] == 2 and c["trip_stops"] == 2 and c["invoice_handover_items"] == 2 and c["trip_events"] == 9
    assert env.zoho_calls == []


def test_out_of_order_rejections(env, crew):
    h = H(crew)
    assert env.client.post(url("start"), headers=h, json=body()).status_code == 409
    r = env.client.post(url("start"), headers=h, json=body())
    assert r.json()["detail"]["code"] == "out_of_order" and "Acknowledge all 2 SOs" in r.json()["detail"]["message"]
    assert env.client.post(url("orders/s1/check-in"), headers=h, json=body()).status_code == 409  # before start
    assert env.client.post(url("end"), headers=h, json=body()).status_code == 409
    assert env.client.post(url("handover"), headers=h, json=body(officer_user_id=1, items=[])).status_code == 409
    ack(env, h, "s1")
    assert env.client.post(url("start"), headers=h, json=body()).status_code == 409  # 1 of 2 acked
    ack(env, h, "s2")
    start_resp = env.client.post(url("start"), headers=h, json=body())
    assert start_resp.status_code == 200
    late = env.client.post(url("orders/s1/acknowledge"), headers=h, json=body(answers=[]))
    assert late.status_code == 200 and late.json()["already_done"] is True  # natural idempotency beats the order check
    assert env.client.post(url("orders/s1/check-out"), headers=h, json=checkout_body()).json()["detail"]["message"] == "Check in at this stop first."
    env.client.post(url("orders/s1/check-in"), headers=h, json=body())
    second = env.client.post(url("orders/s2/check-in"), headers=h, json=body())
    assert second.status_code == 409 and "Check out of SO-s1" in second.json()["detail"]["message"]
    end = env.client.post(url("end"), headers=h, json=body())
    assert end.status_code == 409 and end.json()["detail"]["message"] == "2 stops are not checked out yet."
    assert env.client.get("/api/driver/assignments/b1", headers=h).json()["orders"][1]["workflow"]["blocker"].startswith("Check out of SO-s1")
    assert env.zoho_calls == []


def test_ack_after_start_for_unacked_is_409(env):
    add_order(env, "s1")
    h = H(make_user(env))
    start_trip(env, h)
    add_order(env, "s9", offset=9)  # joins the batch after the start
    r = ack(env, h, "s9")
    assert r.status_code == 409 and r.json()["detail"]["message"] == "The trip has already started."


# ---- idempotency --------------------------------------------------------------------------------------------------------


def test_duplicate_by_helper_returns_already_done_with_done_by(env):
    add_order(env, "s1", helper_ids=[9, 17])
    drv, hlp = make_user(env, "driver", 9, "Drv"), make_user(env, "helper", 17, "Hlp")
    hd, hh = H(drv), H(hlp, "helper")
    assert ack(env, hd, "s1").status_code == 200
    again = ack(env, hh, "s1").json()
    assert again["already_done"] is True and again["done_by"]["name"] == "Drv" and again["done_by"]["role"] == "driver"
    env.client.post(url("start"), headers=hh, json=body())
    dup = env.client.post(url("start"), headers=hd, json=body()).json()
    assert dup["already_done"] and dup["done_by"]["name"] == "Hlp"
    env.client.post(url("orders/s1/check-in"), headers=hd, json=body())
    dup = env.client.post(url("orders/s1/check-in"), headers=hh, json=body()).json()
    assert dup["already_done"] and dup["done_by"]["name"] == "Drv"
    c = counts(env)
    assert c["so_acknowledgements"] == 1 and c["trip_events"] == 3


def test_same_key_replay_returns_stored_result_and_writes_nothing(env, crew):
    h = H(crew)
    b = body(answers=ack_answers(env))
    first = env.client.post(url("orders/s1/acknowledge"), headers=h, json=b).json()
    before = counts(env)
    again = env.client.post(url("orders/s1/acknowledge"), headers=h, json=b).json()
    assert again["replayed"] is True and again["batch"] == first["batch"] and counts(env) == before and before["trip_events"] == 1


def test_concurrent_same_key_race_returns_winner_result(env, crew):
    h = H(crew)
    key = str(uuid.uuid4())
    winner = env.client.post(url("orders/s1/acknowledge"), headers=h, json={**body(answers=ack_answers(env)), "idempotency_key": key}).json()
    env.monkeypatch.setattr(trip_workflow, "_replay", lambda *a: None)  # the pre-check missed the winner (race)
    loser = env.client.post(url("orders/s2/acknowledge"), headers=h, json={**body(answers=ack_answers(env)), "idempotency_key": key})
    assert loser.status_code == 200 and loser.json()["replayed"] is True and loser.json()["batch"] == winner["batch"]
    c = counts(env)
    assert c["so_acknowledgements"] == 1 and c["trip_events"] == 1


def test_offline_queue_replay_twice_creates_no_duplicates(env, crew):
    h = H(crew)
    queue = [("orders/s1/acknowledge", body(answers=ack_answers(env))), ("orders/s2/acknowledge", body(answers=ack_answers(env))), ("start", body()),
             ("orders/s1/check-in", body()), ("orders/s1/check-out", checkout_body()), ("orders/s2/check-in", body()), ("orders/s2/check-out", checkout_body("Delivered")), ("end", body())]
    for _ in range(2):
        for path, payload in queue:
            assert env.client.post(url(path), headers=h, json=payload).status_code == 200
    c = counts(env)
    assert c == {"trips": 1, "so_acknowledgements": 2, "trip_stops": 2, "trip_events": 8, "invoice_handovers": 0, "invoice_handover_items": 0}


# ---- checklist / templates -----------------------------------------------------------------------------------------------


def test_checklist_required_items_and_versioning(env, crew):
    h = H(crew)
    r = env.client.post(url("orders/s1/acknowledge"), headers=h, json=body(answers=[]))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "validation"
    keys = {e["key"] for e in r.json()["detail"]["errors"]}
    assert {"products_match", "invoice_copies", "temp_ok"} <= keys and "remarks" not in keys
    assert counts(env)["trips"] == 0  # a rejected acknowledgement creates nothing
    bad = env.client.post(url("orders/s1/acknowledge"), headers=h, json=body(answers=[{"item_id": 9999, "checked": True}]))
    assert bad.status_code == 422 and any("Unknown" in e["message"] for e in bad.json()["detail"]["errors"])
    neg = ack_answers(env) + [{"item_id": 7, "count": -1}]
    assert env.client.post(url("orders/s1/acknowledge"), headers=h, json=body(answers=neg)).status_code == 422
    with env.Session() as db:  # v2 becomes active; v1 must still be accepted and stored as version 1
        db.get(FormTemplate, 1).active = False
        db.add(FormTemplate(id=3, kind="acknowledge_checklist", version=2, name="v2", active=True))
        db.flush()
        db.add(FormTemplateItem(template_id=3, position=1, key="only", label="Only", item_type="checkbox", required=True, active=True))
        db.commit()
    active = env.client.get("/api/driver/templates", headers=h).json()
    assert active["acknowledge_checklist"]["version"] == 2 and active["photo_storage_enabled"] is False and active["delivery_checkout"]["version"] == 1
    r = env.client.post(url("orders/s1/acknowledge"), headers=h, json=body(template_id=1, answers=ack_answers(env)))
    assert r.status_code == 200
    with env.Session() as db:
        assert db.execute(select(SoAcknowledgement.template_version)).scalar_one() == 1


def test_checkout_required_fields_show_when_and_outcomes(env, crew):
    h = H(crew)
    start_trip(env, h, ("s1", "s2"))
    env.client.post(url("orders/s1/check-in"), headers=h, json=body())
    tpl = env.client.get("/api/driver/templates", headers=h).json()["delivery_checkout"]
    assert next(i for i in tpl["items"] if i["key"] == "outcome")["options"] == ["Delivered", "Partially delivered", "Failed / Returned"]

    def errs(**answers):
        r = env.client.post(url("orders/s1/check-out"), headers=h, json=body(answers=answers))
        assert r.status_code == 422
        return {e["key"] for e in r.json()["detail"]["errors"]}

    assert "outcome" in errs()
    assert "outcome" in errs(outcome="Nope")
    assert errs(outcome="Delivered") == {"received_by"}  # reason NOT required for Delivered
    assert errs(outcome="Partially delivered", received_by="x") == {"reason"}
    assert errs(outcome="Failed / Returned") == {"reason"}  # received_by NOT required
    assert "whatever" in errs(outcome="Delivered", received_by="x", whatever=1)
    assert env.client.post(url("orders/s1/check-out"), headers=h, json=checkout_body("Failed / Returned")).status_code == 200
    d = detail(env, h)
    stop = d["orders"][0]["workflow"]["stop"]
    assert stop["outcome"] == "Failed / Returned" and stop["reason"] == "short"
    assert d["orders"][0]["checkout_prefill"]["so_number"] == "SO-s1" and d["orders"][0]["checkout_prefill"]["driver"] == "Emert"


def test_photo_and_signature_only_required_when_storage_enabled(env, crew):
    h = H(crew)
    with env.Session() as db:
        for key in ("delivery_photo", "signature"):
            db.execute(FormTemplateItem.__table__.update().where(FormTemplateItem.key == key).values(required=True))
        db.commit()
    start_trip(env, h, ("s1", "s2"))
    env.client.post(url("orders/s1/check-in"), headers=h, json=body())
    assert env.client.post(url("orders/s1/check-out"), headers=h, json=checkout_body()).status_code == 200  # storage disabled
    env.monkeypatch.setattr(media_storage, "is_enabled", lambda: True)
    env.client.post(url("orders/s2/check-in"), headers=h, json=body())
    r = env.client.post(url("orders/s2/check-out"), headers=h, json=checkout_body())
    assert r.status_code == 422 and {e["key"] for e in r.json()["detail"]["errors"]} == {"delivery_photo", "signature"}
    with env.Session() as db:
        db.add(TripPhoto(id=5, batch_id="b1", field_key="x", storage_key="k5", content_type="image/png", size_bytes=1, uploaded_by_user_id=1))
        db.add(TripPhoto(id=6, batch_id="other", field_key="x", storage_key="k6", content_type="image/png", size_bytes=1, uploaded_by_user_id=1))
        db.commit()
    r = env.client.post(url("orders/s2/check-out"), headers=h, json=checkout_body(delivery_photo=6, signature=5))
    assert r.status_code == 422 and [e["key"] for e in r.json()["detail"]["errors"]] == ["delivery_photo"]  # photo of another batch
    assert env.client.post(url("orders/s2/check-out"), headers=h, json=checkout_body(delivery_photo=5, signature=5)).status_code == 200


# ---- check-in far flag, stop order, route ---------------------------------------------------------------------------------------


def test_far_checkin_flag_with_and_without_coordinates(env):
    add_order(env, "s1", address={"address": "A", "latitude": 14.5, "longitude": 121.0})
    add_order(env, "s2", offset=1, address={"address": "B", "lat": 14.6, "lng": 121.1})
    add_order(env, "s3", offset=2)  # no coordinates
    h = H(make_user(env))
    start_trip(env, h, ("s1", "s2", "s3"))
    far = env.client.post(url("orders/s1/check-in"), headers=h, json=body(gps={"lat": 14.6, "lng": 121.1, "accuracy_m": 5})).json()
    st = far["batch"]["orders"][0]["workflow"]["stop"]
    assert st["checkin_far"] is True and st["checkin_distance_m"] > 500
    env.client.post(url("orders/s1/check-out"), headers=h, json=checkout_body())
    near = env.client.post(url("orders/s2/check-in"), headers=h, json=body(gps={"lat": 14.6001, "lng": 121.1, "accuracy_m": 5})).json()
    st = near["batch"]["orders"][1]["workflow"]["stop"]
    assert st["checkin_far"] is False and st["checkin_distance_m"] < 50
    env.client.post(url("orders/s2/check-out"), headers=h, json=checkout_body())
    nocoord = env.client.post(url("orders/s3/check-in"), headers=h, json=body(gps={"lat": 1, "lng": 1})).json()["batch"]["orders"][2]["workflow"]["stop"]
    assert nocoord["checkin_far"] is None and nocoord["checkin_distance_m"] is None
    env.client.post(url("orders/s3/check-out"), headers=h, json=checkout_body())
    ended = env.client.post(url("end"), headers=h, json=body(gps={"lat": 14.2907776, "lng": 121.0134132})).json()
    assert ended["ok"]
    with env.Session() as db:
        assert db.execute(select(Trip.end_distance_m)).scalar_one() == 0.0


def test_stop_order_optimized_vs_assignment_and_frozen_at_start(env):
    add_order(env, "s1", route_id="900")
    add_order(env, "s2", offset=1, route_id="900")
    add_order(env, "s3", offset=2, route_id="900")
    h = H(make_user(env))
    assert detail(env, h)["stop_order_set"] is False  # route_id set on the rows but no stored sequence yet
    for seq, so in ((1, "s3"), (2, "s1"), (3, "s2")):
        memory_tables.route_stops.create(route_id=900, so_id=so, sequence=seq)
    d = detail(env, h)
    assert [o["id"] for o in d["orders"]] == ["s3", "s1", "s2"] and d["stop_order_set"] is True
    r = env.client.get(url("route"), headers=h).json()
    assert r["source"] == "optimized" and [s["so_id"] for s in r["stops"]] == ["s3", "s1", "s2"] and r["origin"]["name"] == "Mets Cold Storage"
    start_trip(env, h, ("s1", "s2", "s3"))
    for stop in list(memory_tables.route_stops.list(route_id=900)):  # re-optimizing later must not move a started trip
        memory_tables.route_stops.update(stop["id"], sequence=99 - stop["sequence"])
    r = env.client.get(url("route"), headers=h).json()
    assert [s["so_id"] for s in r["stops"]] == ["s3", "s1", "s2"] and r["source"] == "optimized" and r["stops"][0]["status"] == "pending"
    # one SO missing from the route stops -> assignment order
    add_order(env, "t1", batch="b2", route_id="901")
    add_order(env, "t2", batch="b2", offset=1, route_id="901")
    memory_tables.route_stops.create(route_id=901, so_id="t2", sequence=1)
    r = env.client.get(url("route", "b2"), headers=h).json()
    assert r["source"] == "assignment" and [s["so_id"] for s in r["stops"]] == ["t1", "t2"]


def test_route_maps_links_split_above_nine_intermediates(env):
    for i in range(11):
        add_order(env, f"m{i:02d}", offset=i)
    h = H(make_user(env))
    r = env.client.get(url("route"), headers=h).json()
    assert [l["label"] for l in r["maps_links"]] == ["Part 1 of 2", "Part 2 of 2"]
    assert r["maps_links"][0]["url"].count("|") == 8 and "origin=14.2907776,121.0134132" in r["maps_links"][0]["url"] and "travelmode=driving" in r["maps_links"][0]["url"]
    assert r["stops"][0]["navigate_url"].startswith("https://www.google.com/maps/dir/?api=1&destination=")
    add_order(env, "z1", batch="b3")
    add_order(env, "z2", batch="b3", offset=1)
    assert len(env.client.get(url("route", "b3"), headers=h).json()["maps_links"]) == 1


# ---- handover -------------------------------------------------------------------------------------------------------------


def test_handover_validation(env):
    add_order(env, "s1")
    h = H(make_user(env))
    start_trip(env, h)
    env.client.post(url("orders/s1/check-in"), headers=h, json=body())
    env.client.post(url("orders/s1/check-out"), headers=h, json=checkout_body())
    env.client.post(url("end"), headers=h, json=body())
    officer = make_user(env, "logistics_officer", None, "Off")
    inactive = make_user(env, "logistics_officer", None, "Old", status="disabled")
    other = make_user(env, "dispatcher", None, "Disp")
    ok_items = [{"so_id": "s1", "handed_over": True}]

    def post(**kw):
        return env.client.post(url("handover"), headers=h, json=body(**kw))

    for who in (inactive, other, 99999):
        r = post(officer_user_id=who, items=ok_items)
        assert r.status_code == 422 and r.json()["detail"]["errors"][0]["key"] == "officer_user_id"
    assert post(officer_user_id=officer, items=[{"so_id": "s1", "handed_over": False}]).status_code == 422  # note needed
    assert post(officer_user_id=officer, items=[]).status_code == 422  # every SO must be listed
    assert post(officer_user_id=officer, items=[{"so_id": "nope", "handed_over": True}]).status_code == 422
    assert counts(env)["invoice_handovers"] == 0
    assert post(officer_user_id=officer, items=[{"so_id": "s1", "handed_over": False, "note": "lost"}]).status_code == 200


# ---- crew scoping, roles, cache -------------------------------------------------------------------------------------------


def test_helper_sees_batch_and_id_1_does_not_match_17(env):
    add_order(env, "s1", helper_ids=[9, 17])
    add_order(env, "s2", batch="b2", offset=1, helper_ids=[9, 1])
    h17, h1 = H(make_user(env, "helper", 17), "helper"), H(make_user(env, "helper", 1), "helper")
    assert [b["batch_id"] for b in env.client.get("/api/driver/today", headers=h17).json()["batches"]] == ["b1"]
    assert [b["batch_id"] for b in env.client.get("/api/driver/today", headers=h1).json()["batches"]] == ["b2"]
    assert env.client.get("/api/driver/assignments/b1", headers=h1).status_code == 404
    assert env.client.get("/api/driver/assignments/b1", headers=h17).json()["crew"] == [
        {"id": 9, "name": "Emert", "role": "driver", "phone": "0948"}, {"id": 17, "name": "Raymond", "role": "helper", "phone": "017"}]
    add_order(env, "old", batch="b0", day=driver_assignments.manila_today() - timedelta(days=2), helper_ids=[9, 17])
    hist = env.client.get("/api/driver/history?days=7", headers=h17).json()["batches"]
    assert [b["batch_id"] for b in hist] == ["b0"] and env.client.get("/api/driver/history?days=7", headers=h1).json()["batches"] == []
    assert env.client.post(url("orders/s2/acknowledge"), headers=h17, json=body(answers=ack_answers(env))).status_code == 404
    me = env.client.get("/api/driver/me", headers=h17).json()
    assert me["role"] == "helper" and me["warehouse"] == "METS"


def test_non_crew_officer_dispatcher_admin_rules(env, crew):
    other = H(make_user(env, "driver", 10))
    assert env.client.post(url("start"), headers=other, json=body()).status_code == 404
    assert env.client.get(url("route"), headers=other).status_code == 404
    for role in ("logistics_officer", "dispatcher"):
        assert env.client.post(url("start"), headers=H(make_user(env, role, None), role), json=body()).status_code == 403
    admin = H(make_user(env, "admin", None), "admin")
    r = env.client.post(url("start"), headers=admin, json=body())
    assert r.status_code == 403 and r.json()["detail"]["code"] == "forbidden"
    assert env.client.get("/api/driver/today", headers=admin).status_code == 400
    d = env.client.get("/api/driver/assignments/b1?driver_id=9", headers=admin).json()
    assert d["trip"]["blocker"] and not d["orders"][0]["workflow"]["can_acknowledge"]
    assert env.client.get(url("route") + "?driver_id=9", headers=admin).status_code == 200
    assert counts(env)["trips"] == 0 and env.zoho_calls == []


def test_today_cache_never_stale_after_an_action(env, crew):
    h = H(crew)
    assert env.client.get("/api/driver/today", headers=h).json()["batches"][0]["trip"]["acknowledged"] == 0
    ack(env, h, "s1")
    assert env.client.get("/api/driver/today", headers=h).json()["batches"][0]["trip"]["acknowledged"] == 1
    ack(env, h, "s2")
    env.client.post(url("start"), headers=h, json=body())
    t = env.client.get("/api/driver/today", headers=h).json()["batches"][0]["trip"]
    assert t["status"] == "on_trip" and t["chip"] == "on_trip"
    gen = trip_events.generation()
    assert env.client.post(url("orders/s1/check-in"), headers=h, json=body()).status_code == 200 and trip_events.generation() == gen + 1
    assert env.client.get("/api/driver/today", headers=h).json()["batches"][0]["trip"]["current_stop_so_id"] == "s1"


def test_templates_and_officers_endpoints_roles(env, crew):
    assert env.client.get("/api/driver/templates", headers=H(crew)).json()["acknowledge_checklist"]["items"][0]["key"] == "products_match"
    assert env.client.get("/api/driver/templates").status_code == 401
    assert env.client.get("/api/driver/templates", headers=H(make_user(env, "logistics_officer", None), "logistics_officer")).status_code == 403
