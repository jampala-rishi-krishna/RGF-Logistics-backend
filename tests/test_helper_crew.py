"""Helpers use the driver app: same trips, same actions, first action wins, crew contract, Add User staff picker, one helper per
truck, Driver Overview actor names. SQLite + fake staff directory; zero Zoho, zero network."""
from __future__ import annotations

import csv
import io
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from models.trip import FormTemplateItem, SoAcknowledgement, TripEvent
from models.user import User
from routers import admin, assignment, driver as driver_router, driver_overview, driver_trip
from services import media_storage
from tests import trip_test_support as t
from tests.test_assignment_auto_email import Harness, make_order

DRIVER, HELPER, OTHER_HELPER = 9, 17, 18  # Emert Hirondo (driver), Raymond Roblo / Jaymar Arca (helpers)


@pytest.fixture()
def env(monkeypatch):
    env = t.build_env(monkeypatch, driver_router.router, driver_trip.router, driver_overview.router, admin.router, admin.usage_router)
    monkeypatch.setattr(media_storage, "is_enabled", lambda: False)
    t.seed_templates(env)
    admin._linked_staff.clear()
    env.admin = t.make_user(env, name="Ada Admin", email="ada@example.test", role="admin")
    env.driver = t.make_user(env, name="Emert Hirondo", email="emert@example.test", role="driver", staff_id=DRIVER)
    env.helper = t.make_user(env, name="Raymond Roblo", email="ray@example.test", role="helper", staff_id=HELPER)
    env.outsider = t.make_user(env, name="Jaymar Arca", email="jay@example.test", role="helper", staff_id=OTHER_HELPER)
    env.h = {"admin": t.bearer(env.admin, "admin"), "driver": t.bearer(env.driver, "driver"), "helper": t.bearer(env.helper, "helper"), "outsider": t.bearer(env.outsider, "helper")}
    for so in ("s1", "s2"):
        t.add_so(env, so, batch="b1", driver=DRIVER, helpers=(HELPER,), offset=1 if so == "s2" else 0)
    yield env
    assert env.zoho_calls == []
    env.engine.dispose()


def body(**extra):
    return {"idempotency_key": str(uuid.uuid4()), "client_ts": None, "gps": None, "gps_denied_reason": "denied", **extra}


def ack_body(env):
    with env.Session() as db:
        items = db.execute(select(FormTemplateItem).where(FormTemplateItem.template_id == 1)).scalars().all()
    return body(answers=[{"item_id": i.id, "checked": True} for i in items if i.item_type == "checkbox"])


def post(env, who, path, payload=None):
    return env.client.post(f"/api/driver/batches/b1/{path}", headers=env.h[who], json=payload if payload is not None else body())


def checkout(env, who, so):
    return post(env, who, f"orders/{so}/check-out", body(answers={"outcome": "Delivered", "received_by": "Ana"}))


# ---- visibility ---------------------------------------------------------------------------------------------------------


def test_helper_sees_their_trip_with_crew_contract_and_viewer_role(env):
    today = env.client.get("/api/driver/today", headers=env.h["helper"]).json()
    assert today["viewer_role"] == "helper"
    [batch] = today["batches"]
    assert batch["batch_id"] == "b1" and batch["viewer_role"] == "helper"
    assert batch["crew"] == {"driver": {"staff_id": DRIVER, "name": "Emert Hirondo", "phone": "09482834802"},
                             "helpers": [{"staff_id": HELPER, "name": "Raymond Roblo", "phone": "09170000017"}]}
    assert [c["role"] for c in batch["crew_members"]] == ["driver", "helper"]  # previous list shape kept
    detail = env.client.get("/api/driver/assignments/b1", headers=env.h["helper"]).json()
    assert detail["crew"]["driver"]["phone"] == "09482834802" and detail["viewer_role"] == "helper" and len(detail["orders"]) == 2
    assert env.client.get("/api/driver/batches/b1/timeline", headers=env.h["helper"]).status_code == 200
    me = env.client.get("/api/driver/me", headers=env.h["helper"]).json()
    assert me["viewer_role"] == "helper" and [p["id"] for p in me["crew_today"]] == [DRIVER]


def test_crew_member_not_on_the_trip_gets_404_everywhere(env):
    h = env.h["outsider"]
    assert env.client.get("/api/driver/today", headers=h).json()["batches"] == []
    assert env.client.get("/api/driver/assignments/b1", headers=h).status_code == 404
    assert env.client.get("/api/driver/batches/b1/timeline", headers=h).status_code == 404
    assert post(env, "outsider", "orders/s1/acknowledge", ack_body(env)).status_code == 404
    assert post(env, "outsider", "start").status_code == 404


def test_driver_login_unchanged(env):
    today = env.client.get("/api/driver/today", headers=env.h["driver"]).json()
    assert today["viewer_role"] == "driver" and today["driver"]["id"] == DRIVER
    assert today["batches"][0]["viewer_role"] == "driver" and today["batches"][0]["crew"]["helpers"][0]["staff_id"] == HELPER
    assert env.client.get("/api/driver/me", headers=env.h["driver"]).json()["viewer_role"] == "driver"


# ---- actions ------------------------------------------------------------------------------------------------------------


def test_helper_does_every_crew_action_and_is_named(env):
    for so in ("s1", "s2"):
        r = post(env, "helper", f"orders/{so}/acknowledge", ack_body(env))
        assert r.status_code == 200 and r.json()["already_done"] is False and r.json()["done_by"] == "Raymond Roblo"
    assert post(env, "helper", "start").json()["done_by"] == "Raymond Roblo"
    for so in ("s1", "s2"):
        assert post(env, "helper", f"orders/{so}/check-in").status_code == 200
        r = checkout(env, "helper", so)
        assert r.status_code == 200, r.text
    end = post(env, "helper", "end")
    assert end.status_code == 200 and end.json()["handover"] is not None and end.json()["done_by"] == "Raymond Roblo"
    with env.Session() as db:
        events = db.execute(select(TripEvent).order_by(TripEvent.id)).scalars().all()
    crew_events = [e for e in events if e.event_type != "handover_submitted"]
    assert crew_events and all(e.role == "helper" and e.staff_id == HELPER for e in crew_events)

    # Driver Overview: timeline actor + trip row crew / last action + CSV "done by" / role
    tl = env.client.get("/api/driver-overview/trips/b1", headers=env.h["admin"]).json()
    delivered = [e for e in tl["events"] if e["type"] == "checked_out"]
    assert delivered and all(e["actor_name"] == "Raymond Roblo" and e["actor_role"] == "helper" for e in delivered)
    assert tl["crew_detail"]["driver"]["staff_id"] == DRIVER and tl["crew_detail"]["helpers"][0]["phone"] == "09170000017"
    day = t.today().isoformat()
    rows = env.client.get(f"/api/driver-overview/trips?date={day}", headers=env.h["admin"]).json()["trips"]
    [row] = [r for r in rows if r["batch_id"] == "b1"]
    assert row["helpers_detail"] == [{"staff_id": HELPER, "name": "Raymond Roblo", "phone": "09170000017"}]
    assert row["last_action"]["by"] == "Raymond Roblo" and row["last_action"]["by_role"] == "helper"
    found = env.client.get(f"/api/driver-overview/trips?date={day}&q=raymond", headers=env.h["admin"]).json()["trips"]
    assert [r["batch_id"] for r in found] == ["b1"]  # search matches helper names
    response = env.client.get(f"/api/driver-overview/export.csv?date={day}", headers=env.h["admin"])
    table = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    header, data = table[0], [dict(zip(table[0], r)) for r in table[1:]]
    assert header[-5:] == ["acknowledged_role", "arrived_by", "arrived_role", "done_by", "role"]
    assert data and all(r["done_by"] == "Raymond Roblo" and r["role"] == "helper" and r["acknowledged_by"] == "Raymond Roblo" and r["acknowledged_role"] == "helper" for r in data)


def test_second_crew_member_gets_already_done_with_first_name(env):
    assert post(env, "driver", "orders/s1/acknowledge", ack_body(env)).json()["done_by"] == "Emert Hirondo"
    again = post(env, "helper", "orders/s1/acknowledge", ack_body(env))
    assert again.status_code == 200 and again.json()["already_done"] is True and again.json()["done_by"] == "Emert Hirondo"
    assert post(env, "helper", "orders/s2/acknowledge", ack_body(env)).status_code == 200
    assert post(env, "helper", "start").json()["already_done"] is False
    dup = post(env, "driver", "start").json()
    assert dup["already_done"] is True and dup["done_by"] == "Raymond Roblo" and dup["done_by_detail"]["role"] == "helper"
    assert post(env, "helper", "orders/s1/check-in").status_code == 200
    dup = post(env, "driver", "orders/s1/check-in").json()
    assert dup["already_done"] is True and dup["done_by"] == "Raymond Roblo"
    bulk = post(env, "driver", "acknowledge-all", body())
    assert bulk.json()["already_done"] is True and bulk.json()["done_by"] == "Emert Hirondo"
    with env.Session() as db:
        assert db.scalar(select(func.count()).select_from(SoAcknowledgement)) == 2
        assert db.scalar(select(func.count()).select_from(TripEvent)) == 4  # 2 acks + start + check-in: no duplicates


# ---- admin Add User -----------------------------------------------------------------------------------------------------


def test_staff_options_filter_by_role(env):
    env.client.get("/users", headers=env.h["admin"])  # the Users page list the admin already loads (remembers linked staff ids)
    helpers = env.client.get("/api/admin/staff-options?role=helper", headers=env.h["admin"]).json()
    assert [(o["staff_id"], o["role"]) for o in helpers] == [(OTHER_HELPER, "helper"), (HELPER, "helper")]  # sorted by name
    assert {o["staff_id"]: o["linked_user_id"] for o in helpers} == {HELPER: env.helper, OTHER_HELPER: env.outsider}
    drivers = env.client.get("/api/admin/staff-options?role=DRIVER", headers=env.h["admin"]).json()
    assert {o["staff_id"] for o in drivers} == {9, 10, 11, 12} and all(o["role"] == "driver" and "phone" in o for o in drivers)  # 13 is inactive
    assert env.client.get("/api/admin/staff-options?role=admin", headers=env.h["admin"]).status_code == 400
    assert env.client.get("/api/admin/staff-options?role=helper", headers=env.h["helper"]).status_code == 403


def test_create_helper_user_requires_and_links_staff_record(env):
    def create(**extra):
        return env.client.post("/api/admin/users", headers=env.h["admin"], json={"full_name": "New Helper", "email": f"{uuid.uuid4().hex[:8]}@example.test", "role": "helper", **extra})

    missing = create()
    assert missing.status_code == 400 and missing.json()["detail"] == "Choose a staff record"
    assert create(staff_id=10).status_code == 400  # a driver record cannot be linked to a helper login
    with env.Session() as db:
        db.delete(db.get(User, env.outsider))
        db.commit()
    created = create(staff_id=OTHER_HELPER)
    assert created.status_code == 201 and created.json()["staff_id"] == OTHER_HELPER and created.json()["role"] == "helper"
    options = env.client.get("/api/admin/staff-options?role=helper", headers=env.h["admin"]).json()
    assert {o["staff_id"]: o["linked_user_id"] for o in options}[OTHER_HELPER] == created.json()["id"]


# ---- Load Planning: one helper per truck ----------------------------------------------------------------------------------


def test_assignment_rejects_more_than_one_helper(monkeypatch):
    h = Harness(monkeypatch)

    order = make_order("SO-9")
    h.orders = {order.id: order}
    too_many = assignment.AssignmentBody(salesorder_ids=[order.id], vehicle_id="TRK1", driver_id=1, helper_ids=[2, 3])
    with pytest.raises(HTTPException) as exc:
        assignment.assign_order(order.id, too_many, h.user, h.db)
    assert exc.value.status_code == 400 and exc.value.detail == "Only one helper per truck"
    assert order.assignment_status == "unassigned"  # nothing was changed
    ok = assignment.assign_order(order.id, assignment.AssignmentBody(salesorder_ids=[order.id], vehicle_id="TRK1", driver_id=1, helper_ids=[2]), h.user, h.db)
    assert ok["helper_ids"] == [2]


def test_stored_assignment_with_two_helpers_still_reads(env):
    t.add_so(env, "s9", batch="b9", driver=DRIVER, helpers=(HELPER, OTHER_HELPER))
    detail = env.client.get("/api/driver/assignments/b9", headers=env.h["outsider"])
    assert detail.status_code == 200 and [p["staff_id"] for p in detail.json()["crew"]["helpers"]] == [HELPER, OTHER_HELPER]
