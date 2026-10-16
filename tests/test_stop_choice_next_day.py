"""The crew delivers a started trip's stops in any order they choose, and sees tomorrow's assignments today (acknowledge the day
before, Start trip on the delivery day). SQLite + fake staff directory; zero Zoho, zero network."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from models.trip import FormTemplateItem
from routers import driver as driver_router, driver_trip
from services import media_storage
from tests import trip_test_support as t

DRIVER, HELPER = 9, 17


@pytest.fixture()
def env(monkeypatch):
    env = t.build_env(monkeypatch, driver_router.router, driver_trip.router)
    monkeypatch.setattr(media_storage, "is_enabled", lambda: False)
    t.seed_templates(env)
    env.driver = t.make_user(env, name="Emert Hirondo", email="emert@example.test", role="driver", staff_id=DRIVER)
    env.helper = t.make_user(env, name="Raymond Roblo", email="ray@example.test", role="helper", staff_id=HELPER)
    env.h = {"driver": t.bearer(env.driver, "driver"), "helper": t.bearer(env.helper, "helper")}
    yield env
    assert env.zoho_calls == []
    env.engine.dispose()


def body(**extra):
    return {"idempotency_key": str(uuid.uuid4()), "client_ts": None, "gps": None, "gps_denied_reason": "denied", **extra}


def ack_body(env):
    with env.Session() as db:
        items = db.execute(select(FormTemplateItem).where(FormTemplateItem.template_id == 1)).scalars().all()
    return body(answers=[{"item_id": i.id, "checked": True} for i in items if i.item_type == "checkbox"])


def post(env, who, batch, path, payload=None):
    return env.client.post(f"/api/driver/batches/{batch}/{path}", headers=env.h[who], json=payload if payload is not None else body())


def checkout(env, who, batch, so):
    return post(env, who, batch, f"orders/{so}/check-out", body(answers={"outcome": "Delivered", "received_by": "Ana"}))


def ack_all(env, batch, sos):
    for so in sos:
        assert post(env, "helper", batch, f"orders/{so}/acknowledge", ack_body(env)).status_code == 200


def orders_of(env, batch):
    return {o["stop_id"]: o for o in env.client.get(f"/api/driver/assignments/{batch}", headers=env.h["helper"]).json()["orders"]}


# ---- change 1: any delivery order -----------------------------------------------------------------------------------------


def test_crew_delivers_stop_3_before_stop_1(env):
    for i, so in enumerate(("s1", "s2", "s3")):
        t.add_so(env, so, batch="b1", driver=DRIVER, helpers=(HELPER,), offset=i)
    ack_all(env, "b1", ("s1", "s2", "s3"))
    assert post(env, "helper", "b1", "start").status_code == 200
    orders = orders_of(env, "b1")
    assert {o["status"] for o in orders.values()} == {"pending"} and not any(o["is_active"] for o in orders.values())
    assert all(o["workflow"]["can_check_in"] for o in orders.values())  # every pending stop is choosable

    last = max(orders.values(), key=lambda o: o["workflow"]["stop"]["sequence"])["stop_id"]
    first = min(orders.values(), key=lambda o: o["workflow"]["stop"]["sequence"])["stop_id"]
    assert post(env, "helper", "b1", f"orders/{last}/check-in").status_code == 200
    orders = orders_of(env, "b1")
    assert orders[last]["is_active"] and orders[last]["status"] == "checked_in"
    assert [so for so, o in orders.items() if o["is_active"]] == [last]
    assert checkout(env, "helper", "b1", last).status_code == 200
    assert orders_of(env, "b1")[last]["status"] == "checked_out"

    assert post(env, "driver", "b1", f"orders/{first}/check-in").status_code == 200
    assert checkout(env, "driver", "b1", first).status_code == 200
    middle = ({"s1", "s2", "s3"} - {first, last}).pop()
    assert post(env, "helper", "b1", f"orders/{middle}/check-in").status_code == 200
    assert checkout(env, "helper", "b1", middle).status_code == 200
    assert post(env, "helper", "b1", "end").status_code == 200

    # idempotency rules unchanged: a repeated check-in is already_done
    again = post(env, "driver", "b1", f"orders/{last}/check-in")
    assert again.status_code == 200 and again.json()["already_done"] is True


def test_switching_stop_reopens_the_previous_one(env):
    for i, so in enumerate(("s1", "s2")):
        t.add_so(env, so, batch="b1", driver=DRIVER, helpers=(HELPER,), offset=i)
    ack_all(env, "b1", ("s1", "s2"))
    post(env, "helper", "b1", "start")
    assert post(env, "helper", "b1", "orders/s2/check-in").status_code == 200
    assert post(env, "helper", "b1", "orders/s1/check-in").status_code == 200
    orders = orders_of(env, "b1")
    assert orders["s1"]["status"] == "checked_in" and orders["s2"]["status"] == "pending"
    assert [so for so, o in orders.items() if o["is_active"]] == ["s1"]


# ---- change 2: tomorrow's assignments ---------------------------------------------------------------------------------------


def test_tomorrow_listed_acknowledgeable_but_not_startable(env):
    tomorrow = t.today() + timedelta(days=1)
    t.add_so(env, "t1", batch="today-b", driver=DRIVER, helpers=(HELPER,))
    t.add_so(env, "n1", batch="next-b", driver=DRIVER, helpers=(HELPER,), day=tomorrow)
    t.add_so(env, "far", batch="far-b", driver=DRIVER, helpers=(HELPER,), day=tomorrow + timedelta(days=1))

    today = env.client.get("/api/driver/today", headers=env.h["helper"]).json()
    by_id = {b["batch_id"]: b for b in today["batches"]}
    assert list(by_id) == ["today-b", "next-b"]  # today first; nothing beyond tomorrow
    assert by_id["today-b"]["day"] == "today" and by_id["today-b"]["delivery_date"] == t.today().isoformat()
    assert by_id["next-b"]["day"] == "tomorrow" and by_id["next-b"]["delivery_date"] == tomorrow.isoformat()
    assert today["summary"]["assigned"] == 1 and today["tomorrow"] == tomorrow.isoformat()

    ack_all(env, "next-b", ("n1",))
    detail = env.client.get("/api/driver/assignments/next-b", headers=env.h["helper"]).json()
    assert detail["day"] == "tomorrow" and detail["trip"]["acknowledged"] == 1
    assert detail["trip"]["can_start"] is False and tomorrow.isoformat() in detail["trip"]["blocker"]

    refused = post(env, "helper", "next-b", "start")
    assert refused.status_code == 400
    assert refused.json()["detail"] == {"message": f"This trip is for {tomorrow.isoformat()}; start it on that day", "code": "not_today"}

    ack_all(env, "today-b", ("t1",))
    assert env.client.get("/api/driver/assignments/today-b", headers=env.h["helper"]).json()["trip"]["can_start"] is True
    assert post(env, "helper", "today-b", "start").status_code == 200


def test_me_counts_only_todays_truck(env):
    t.add_so(env, "n1", batch="next-b", driver=DRIVER, helpers=(HELPER,), day=t.today() + timedelta(days=1), vehicle="TMR9999")
    t.add_so(env, "t1", batch="today-b", driver=DRIVER, helpers=(HELPER,))
    me = env.client.get("/api/driver/me", headers=env.h["helper"]).json()
    assert me["vehicles_today"] == ["NAA1234"]
