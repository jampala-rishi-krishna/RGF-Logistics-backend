"""The whole crew + officer flow through the REAL routes and services (in-memory SQLite, R2 mocked): acknowledge -> start trip -> Arrived ->
Departed with proof of delivery -> end trip -> handover -> officer validation -> what Driver Overview shows. Proof of delivery is required
for Delivered / Partially delivered (a photo), optional for Failed (reason required)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from routers import driver as driver_router, driver_extra, driver_overview, driver_trip, media, officer
from services import media_storage, overview
from tests import trip_test_support as t

R2 = {"R2_ACCOUNT_ID": "acct123", "R2_ACCESS_KEY_ID": "AKIATESTKEY", "R2_SECRET_ACCESS_KEY": "not-a-real-secret", "R2_BUCKET": "trip-photos"}
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

# The template the migration r8d9e0f1a2b3 produces (the seeded draft in the test database is replaced by the same items).
ITEMS = [
    {"key": "invoice_dr_number", "label": "Invoice or DR Number", "item_type": "numeric_text", "required": True},
    {"key": "outcome", "label": "Delivery outcome", "item_type": "radio", "required": True, "options": ["Delivered", "Partially delivered", "Failed"], "app_only": True},
    {"key": "reason", "label": "Reason", "item_type": "textarea", "required": True, "show_when": {"key": "outcome", "in": ["Partially delivered", "Failed"]}, "app_only": True},
    {"key": "delivery_photo", "label": "Proof of delivery photo", "item_type": "photo", "required": True, "show_when": {"key": "outcome", "in": ["Delivered", "Partially delivered"]}, "app_only": True},
    {"key": "failure_photo", "label": "Photo (optional)", "item_type": "photo", "required": False, "show_when": {"key": "outcome", "in": ["Failed"]}, "app_only": True},
    {"key": "notes", "label": "Notes (optional)", "item_type": "textarea", "required": False, "app_only": True},
]


def key() -> str:
    return str(uuid.uuid4())


def stamp(**extra):
    return {"idempotency_key": key(), "client_ts": datetime.now(timezone.utc).isoformat(), "gps": {"lat": 14.5, "lng": 121.0, "accuracy_m": 9}, **extra}


@pytest.fixture()
def env(monkeypatch):
    env = t.build_env(monkeypatch, driver_router.router, driver_trip.router, driver_extra.router, driver_overview.router, officer.router, media.router)
    t.seed_templates(env)
    env.admin = t.make_user(env, name="Ada Admin", email="ada@example.test", role="admin")
    env.dispatcher = t.make_user(env, name="Dee Dispatcher", email="dee@example.test", role="dispatcher")
    env.driver = t.make_user(env, name="Emert", email="emert@example.test", role="driver", staff_id=9)
    env.helper = t.make_user(env, name="Raymond", email="ray@example.test", role="helper", staff_id=17)
    env.officer = t.make_user(env, name="Olivia Officer", email="olivia@example.test", role="logistics_officer")
    env.h = {n: t.bearer(getattr(env, n), role) for n, role in (("admin", "admin"), ("dispatcher", "dispatcher"), ("driver", "driver"), ("helper", "helper"), ("officer", "logistics_officer"))}
    with env.Session() as db:
        overview.create_template_version(db, "delivery_checkout", env.admin, "Delivery time log", ITEMS)
    for name, value in R2.items():
        monkeypatch.setenv(name, value)
    env.puts = []
    monkeypatch.setattr(httpx, "put", lambda url, **kw: env.puts.append(url) or SimpleNamespace(status_code=200))
    monkeypatch.setattr(media_storage, "presign_get", lambda storage_key, expires=600, **kw: f"https://r2.example.test/{storage_key}?sig=test")
    for so, number in (("s1", "SO-1"), ("s2", "SO-2"), ("s3", "SO-3")):
        t.add_so(env, so, number=number, customer=f"Customer {so}", batch="B1", driver=9, helpers=(17,), vehicle="DCD8954", offset=int(so[1]))
    yield env
    assert env.zoho_calls == []
    env.engine.dispose()


def post(env, who, path, body):
    return env.client.post(path, headers=env.h[who], json=body)


def upload(env, who, so, upload_key, field="delivery_photo"):
    return env.client.post("/api/driver/uploads", headers=env.h[who], files={"file": ("pod.jpg", JPEG, "image/jpeg")}, data={"batch_id": "B1", "so_id": so, "field_key": field, "upload_key": upload_key})


def acknowledge_all(env, who="driver"):
    templates = env.client.get("/api/driver/templates", headers=env.h[who]).json()
    checklist = templates["acknowledge_checklist"]
    answers = [{"item_id": i["id"], "checked": True if i["item_type"] == "checkbox" else None, "count": None, "text": None, "photo_id": None, "note": None} for i in checklist["items"] if i["item_type"] == "checkbox"]
    for so in ("s1", "s2", "s3"):
        response = post(env, who, f"/api/driver/batches/B1/orders/{so}/acknowledge", stamp(template_id=checklist["id"], answers=answers))
        assert response.status_code == 200, response.text
    return response.json()["batch"]


def test_the_full_trip_from_acknowledge_to_officer_validation(env):
    # 1. acknowledge every SO: only then can the trip start
    early = post(env, "driver", "/api/driver/batches/B1/start", stamp())
    assert early.status_code == 409 and "Acknowledge all 3 SOs" in early.json()["detail"]["message"]
    batch = acknowledge_all(env)
    assert batch["trip"]["next_action"] == "start" and batch["trip"]["can_start"] is True

    # 2. start the trip (the helper sees the same state)
    started = post(env, "driver", "/api/driver/batches/B1/start", stamp())
    assert started.status_code == 200 and started.json()["batch"]["trip"]["status"] == "on_trip"
    assert env.client.get("/api/driver/assignments/B1", headers=env.h["helper"]).json()["trip"]["next_action"] == "check_in"

    # 3. stop 1: Arrived -> Departed. Delivered needs proof of delivery.
    first = started.json()["batch"]["orders"][0]["id"]
    assert post(env, "driver", f"/api/driver/batches/B1/orders/{first}/check-in", stamp()).status_code == 200
    no_photo = post(env, "driver", f"/api/driver/batches/B1/orders/{first}/check-out", stamp(answers={"invoice_dr_number": "000123", "outcome": "Delivered"}))
    assert no_photo.status_code == 422
    assert [e["key"] for e in no_photo.json()["detail"]["errors"]] == ["delivery_photo"]
    assert "photo is required" in no_photo.json()["detail"]["errors"][0]["message"]
    photo = upload(env, "driver", first, "u-pod-1")
    assert photo.status_code == 200 and len(env.puts) == 1
    done = post(env, "driver", f"/api/driver/batches/B1/orders/{first}/check-out", stamp(answers={"invoice_dr_number": "000123", "outcome": "Delivered", "delivery_photo": photo.json()["photo_id"]}))
    assert done.status_code == 200, done.text
    stop = next(o for o in done.json()["batch"]["orders"] if o["id"] == first)["workflow"]["stop"]
    assert stop["outcome"] == "Delivered" and stop["invoice_no"] == "000123"

    # 4. stop 2: Partially delivered also needs the photo AND a reason
    second = done.json()["batch"]["orders"][1]["id"]
    post(env, "driver", f"/api/driver/batches/B1/orders/{second}/check-in", stamp())
    partial_bad = post(env, "driver", f"/api/driver/batches/B1/orders/{second}/check-out", stamp(answers={"invoice_dr_number": "000124", "outcome": "Partially delivered"}))
    assert {e["key"] for e in partial_bad.json()["detail"]["errors"]} == {"reason", "delivery_photo"}
    photo2 = upload(env, "driver", second, "u-pod-2")
    ok2 = post(env, "driver", f"/api/driver/batches/B1/orders/{second}/check-out", stamp(answers={"invoice_dr_number": "000124", "outcome": "Partially delivered", "reason": "2 cartons short", "delivery_photo": photo2.json()["photo_id"]}))
    assert ok2.status_code == 200, ok2.text

    # 5. stop 3: Failed needs a reason; a photo is optional
    third = ok2.json()["batch"]["orders"][2]["id"]
    post(env, "driver", f"/api/driver/batches/B1/orders/{third}/check-in", stamp())
    failed_bad = post(env, "driver", f"/api/driver/batches/B1/orders/{third}/check-out", stamp(answers={"invoice_dr_number": "000125", "outcome": "Failed"}))
    assert [e["key"] for e in failed_bad.json()["detail"]["errors"]] == ["reason"]
    ok3 = post(env, "driver", f"/api/driver/batches/B1/orders/{third}/check-out", stamp(answers={"invoice_dr_number": "000125", "outcome": "Failed", "reason": "Store closed"}))
    assert ok3.status_code == 200 and ok3.json()["batch"]["trip"]["next_action"] == "end"

    # 6. end the trip: the handover of every carbon copy is created automatically (no driver form)
    ended = post(env, "driver", "/api/driver/batches/B1/end", stamp()).json()
    assert ended["batch"]["trip"]["next_action"] == "wait_officer" and ended["batch"]["trip"]["status"] == "handover_submitted"
    assert sorted((i["so_number"], i["invoice_no"], i["status"]) for i in ended["handover"]["items"]) == [("SO-1", "000123", "pending"), ("SO-2", "000124", "pending"), ("SO-3", "000125", "pending")]
    handed = post(env, "driver", "/api/driver/batches/B1/handover", stamp(officer_user_id=env.officer, items=[{"so_id": so, "handed_over": True} for so in ("s1", "s2", "s3")]))
    assert handed.status_code == 200 and handed.json()["already_done"] is True and handed.json()["handover"]["id"] == ended["handover"]["id"]  # no duplicate

    # 7. the officer validates every line (SO number + Invoice/DR number)
    queue = env.client.get("/api/officer/handovers", headers=env.h["officer"]).json()
    detail = env.client.get(f"/api/officer/handovers/{queue[0]['handover_id']}", headers=env.h["officer"]).json()
    assert sorted((i["so_number"], i["invoice_no"]) for i in detail["items"]) == [("SO-1", "000123"), ("SO-2", "000124"), ("SO-3", "000125")]
    verdict = post(env, "officer", f"/api/officer/handovers/{detail['handover_id']}/validate", stamp(items=[{"item_id": i["item_id"], "status": "received"} for i in detail["items"]]))
    assert verdict.status_code == 200, verdict.text

    # 8. what dispatch sees: everything, nothing hidden
    timeline = env.client.get("/api/driver-overview/trips/B1", headers=env.h["dispatcher"]).json()
    assert timeline["trip"]["status"] == "closed" and timeline["trip"]["started_at"] and timeline["trip"]["duration_minutes"] is not None
    by_so = {o["so_number"]: o for o in timeline["orders"]}
    one = by_so["SO-1"]
    assert one["ack"]["by"]["name"] and one["ack"]["at"] and len(one["ack"]["answers"]) >= 5  # who, when, every checklist answer
    assert one["stop"]["checkin"]["at"] and one["stop"]["checkin"]["gps"]["lat"] == 14.5 and one["stop"]["checkout"]["invoice_no"] == "000123"
    photos = [a for a in one["stop"]["checkout"]["answers"] if a.get("photo_url")]
    assert len(photos) == 1 and photos[0]["photo_url"].startswith("/api/media/")
    assert one["handover"]["officer_status"] == "received" and by_so["SO-3"]["stop"]["checkout"]["outcome"] == "Failed" and by_so["SO-2"]["stop"]["checkout"]["reason"] == "2 cartons short"
    # the dispatcher may open the photo (302 to a short-lived link); the other crew-less role may not
    assert env.client.get(photos[0]["photo_url"], headers=env.h["dispatcher"], follow_redirects=False).status_code == 302
    assert env.client.get(photos[0]["photo_url"], headers=env.h["helper"], follow_redirects=False).status_code == 302  # crew of the batch


def test_without_r2_the_photo_cannot_be_required_and_the_app_is_told_so(env, monkeypatch):
    for name in R2:
        monkeypatch.delenv(name, raising=False)
    templates = env.client.get("/api/driver/templates", headers=env.h["driver"]).json()
    assert templates["photo_storage_enabled"] is False  # the app shows "photo upload not configured", it does not hide the problem
    assert {i["key"] for i in templates["delivery_checkout"]["items"]} >= {"delivery_photo", "failure_photo"}  # the items are still in the template
    acknowledge_all(env)
    post(env, "driver", "/api/driver/batches/B1/start", stamp())
    post(env, "driver", "/api/driver/batches/B1/orders/s1/check-in", stamp())
    response = post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", stamp(answers={"invoice_dr_number": "1", "outcome": "Delivered"}))
    assert response.status_code == 200  # the server cannot demand a photo it cannot store; the app warns the crew and dispatch sees no photo
    assert upload(env, "driver", "s1", "u-x").status_code == 503
