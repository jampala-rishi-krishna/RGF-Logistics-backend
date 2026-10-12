"""Officer flow: handover queue -> validate -> closed, outstanding invoices, roles, idempotency, validation errors."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from models.trip import InvoiceHandover, InvoiceHandoverItem, Trip, TripEvent
from routers import officer
from services import trip_events
from tests import trip_test_support as t


@pytest.fixture()
def env(monkeypatch):
    env = t.build_env(monkeypatch, officer.router)
    env.officer = t.make_user(env, name="Olivia Officer", email="olivia@example.test", role="logistics_officer")
    env.officer2 = t.make_user(env, name="Omar Officer", email="omar@example.test", role="logistics_officer")
    env.admin = t.make_user(env, name="Ada Admin", email="ada@example.test", role="admin")
    env.dispatcher = t.make_user(env, name="Dee Dispatcher", email="dee@example.test", role="dispatcher")
    env.driver = t.make_user(env, name="Emert", email="emert@example.test", role="driver", staff_id=9)
    env.helper = t.make_user(env, name="Raymond", email="ray@example.test", role="helper", staff_id=17)
    env.h = {name: t.bearer(getattr(env, name), role) for name, role in (("officer", "logistics_officer"), ("officer2", "logistics_officer"), ("admin", "admin"), ("dispatcher", "dispatcher"), ("driver", "driver"), ("helper", "helper"))}
    yield env
    assert env.zoho_calls == []
    env.engine.dispose()


def key() -> str:
    return str(uuid.uuid4())


def seed_returned_handover(env, batch="batch-1", *, day=None, statuses=("pending", "pending", "pending"), submitted=None, officer_user_id=None, driver=9, helpers=(17,)):
    day = day or t.today()
    trip_id = t.add_trip(env, batch, status="handover_submitted", day=day, driver=driver, helpers=helpers, started=t.utc(day, 8), ended=t.utc(day, 15))
    items = [(f"{batch}-so{i}", f"SO-{batch}-{i}", f"Customer {i}", i != 3, status) for i, status in enumerate(statuses, 1)]
    for so_id, number, customer, *_ in items:
        t.add_so(env, so_id, number=number, customer=customer, batch=batch, driver=driver, helpers=helpers, day=day)
        t.add_stop(env, trip_id, so_id, 1, number=number, customer=customer, status="checked_out", checkin=t.utc(day, 9), checkout=t.utc(day, 10), outcome="Delivered", user_id=env.driver)
    handover_id, item_ids = t.add_handover(env, trip_id, batch, items, officer_user_id=officer_user_id or env.officer, submitted=submitted or datetime.now(timezone.utc))
    return trip_id, handover_id, item_ids


def validate(env, who, handover_id, item_ids, statuses, notes=None, idem=None):
    notes = notes or {}
    return env.client.post(f"/api/officer/handovers/{handover_id}/validate", headers=env.h[who], json={
        "idempotency_key": idem or key(), "client_ts": datetime.now(timezone.utc).isoformat(),
        "gps": {"lat": 14.3, "lng": 121.0, "accuracy_m": 10}, "items": [{"item_id": i, "status": s, "note": notes.get(n)} for n, (i, s) in enumerate(zip(item_ids, statuses))]})


def events_of(env, batch="batch-1"):
    with env.Session() as db:
        return [e.event_type for e in db.execute(select(TripEvent).where(TripEvent.batch_id == batch).order_by(TripEvent.id)).scalars().all()]


def test_queue_detail_and_for_me(env):
    trip_id, handover_id, item_ids = seed_returned_handover(env)
    rows = env.client.get("/api/officer/handovers", headers=env.h["officer"]).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["handover_id"] == handover_id and row["status"] == "pending" and row["for_me"] is True
    assert row["driver"] == {"id": 9, "name": "Emert Hirondo"} and row["helpers"] == [{"id": 17, "name": "Raymond Roblo"}] and row["vehicle"] == "NAA1234"
    assert row["totals"] == {"invoices": 3, "handed_over": 2, "not_handed_over": 1, "received": 0, "missing": 0, "discrepancy": 0, "pending": 3}
    assert env.client.get("/api/officer/handovers", headers=env.h["officer2"]).json()[0]["for_me"] is False
    detail = env.client.get(f"/api/officer/handovers/{handover_id}", headers=env.h["officer"]).json()
    assert [i["so_number"] for i in detail["items"]] == ["SO-batch-1-1", "SO-batch-1-2", "SO-batch-1-3"]
    assert detail["items"][2]["handed_over"] is False  # the crew said not handed over: still needs an explicit officer status
    assert [s["so_number"] for s in detail["stops"]] == ["SO-batch-1-1", "SO-batch-1-2", "SO-batch-1-3"] or len(detail["stops"]) == 3
    assert env.client.get("/api/officer/handovers/999", headers=env.h["officer"]).status_code == 404
    assert env.client.get("/api/officer/handovers?status=bogus", headers=env.h["officer"]).status_code == 422


def test_all_received_closes_handover_and_trip(env):
    trip_id, handover_id, item_ids = seed_returned_handover(env)
    response = validate(env, "officer", handover_id, item_ids, ["received"] * 3)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] and not body["already_done"] and not body["replayed"] and body["handover"]["status"] == "closed"
    assert body["handover"]["totals"]["received"] == 3
    with env.Session() as db:
        trip = db.get(Trip, trip_id)
        assert trip.status == "closed" and trip.closed_at is not None
        assert db.get(InvoiceHandover, handover_id).officer_acknowledged_by_user_id == env.officer
    types = events_of(env)
    assert types.count("officer_acknowledged") == 1 and types.count("invoice_validated") == 3 and types.count("trip_closed") == 1
    assert env.client.get("/api/officer/handovers", headers=env.h["officer"]).json() == []  # nothing pending any more
    closed = env.client.get("/api/officer/handovers?status=closed", headers=env.h["officer"]).json()
    assert [r["handover_id"] for r in closed] == [handover_id]
    assert env.client.get("/api/officer/outstanding", headers=env.h["officer"]).json() == []


def test_outstanding_path_missing_stays_then_resolve_closes(env):
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    trip_id, handover_id, item_ids = seed_returned_handover(env, submitted=old)
    response = validate(env, "officer", handover_id, item_ids, ["received", "missing", "discrepancy"], notes={1: "customer kept it", 2: "total differs"})
    assert response.status_code == 200, response.text
    assert response.json()["handover"]["status"] == "acknowledged"
    with env.Session() as db:
        assert db.get(Trip, trip_id).status == "handover_submitted" and db.get(Trip, trip_id).closed_at is None
    assert "trip_closed" not in events_of(env)
    pending = env.client.get("/api/officer/handovers", headers=env.h["officer"]).json()
    assert [r["status"] for r in pending] == ["acknowledged"]  # acknowledged with open items stays in the queue

    out = env.client.get("/api/officer/outstanding", headers=env.h["officer"]).json()
    assert [(o["so_number"], o["officer_status"]) for o in out] == [("SO-batch-1-2", "missing"), ("SO-batch-1-3", "discrepancy")]
    assert out[0]["age_hours"] >= 29.9 and out[0]["driver"]["name"] == "Emert Hirondo" and out[0]["officer_note"] == "customer kept it"

    missing_id = item_ids[1]
    resolve = env.client.post(f"/api/officer/invoices/{missing_id}/resolve", headers=env.h["officer"], json={"idempotency_key": key(), "status": "received", "note": "found it"})
    assert resolve.status_code == 200 and resolve.json()["handover"]["status"] == "acknowledged"
    with env.Session() as db:
        item = db.get(InvoiceHandoverItem, missing_id)
        assert item.officer_status == "received" and item.resolved_at is not None and item.officer_checked_at is not None and item.officer_checked_at < item.resolved_at or item.officer_checked_at == item.resolved_at
        assert db.get(Trip, trip_id).status == "handover_submitted"
    assert [o["so_number"] for o in env.client.get("/api/officer/outstanding", headers=env.h["officer"]).json()] == ["SO-batch-1-3"]

    last = env.client.post(f"/api/officer/invoices/{item_ids[2]}/resolve", headers=env.h["officer"], json={"idempotency_key": key(), "status": "received"})
    assert last.status_code == 200 and last.json()["handover"]["status"] == "closed"
    with env.Session() as db:
        assert db.get(Trip, trip_id).status == "closed" and db.get(Trip, trip_id).closed_at is not None
    types = events_of(env)
    assert types.count("invoice_resolved") == 2 and types.count("trip_closed") == 1
    assert env.client.get("/api/officer/outstanding", headers=env.h["officer"]).json() == []


def test_resolved_item_keeps_original_check_time(env):
    trip_id, handover_id, item_ids = seed_returned_handover(env)
    validate(env, "officer", handover_id, item_ids, ["received", "missing", "received"], notes={1: "lost"})
    with env.Session() as db:
        checked = db.get(InvoiceHandoverItem, item_ids[1]).officer_checked_at
    env.client.post(f"/api/officer/invoices/{item_ids[1]}/resolve", headers=env.h["officer"], json={"idempotency_key": key(), "status": "received"})
    with env.Session() as db:
        item = db.get(InvoiceHandoverItem, item_ids[1])
        assert item.officer_checked_at == checked and item.resolved_at is not None


def test_roles_officer_cannot_use_crew_actions_and_crew_cannot_use_officer(env):
    trip_id, handover_id, item_ids = seed_returned_handover(env)
    # officers have no crew rights: the crew action routes are not theirs (403 from the crew routers' guard; here they are not mounted at all -> 404)
    assert env.client.post("/api/driver/batches/batch-1/start", headers=env.h["officer"], json={}).status_code in (403, 404)
    for who in ("driver", "helper"):
        assert env.client.get("/api/officer/handovers", headers=env.h[who]).status_code == 403
        assert env.client.get("/api/officer/outstanding", headers=env.h[who]).status_code == 403
        assert env.client.get("/api/officer/history", headers=env.h[who]).status_code == 403
        assert validate(env, who, handover_id, item_ids, ["received"] * 3).status_code == 403
        assert env.client.post(f"/api/officer/invoices/{item_ids[0]}/resolve", headers=env.h[who], json={"status": "received"}).status_code == 403
    assert env.client.get("/api/officer/handovers").status_code == 401
    with env.Session() as db:
        assert db.get(InvoiceHandover, handover_id).status == "pending"  # nothing changed


def test_dispatcher_is_read_only_and_admin_may_act(env):
    trip_id, handover_id, item_ids = seed_returned_handover(env)
    for path in ("/api/officer/handovers", f"/api/officer/handovers/{handover_id}", "/api/officer/outstanding", "/api/officer/history"):
        assert env.client.get(path, headers=env.h["dispatcher"]).status_code == 200, path
    assert validate(env, "dispatcher", handover_id, item_ids, ["received"] * 3).status_code == 403
    assert env.client.post(f"/api/officer/invoices/{item_ids[0]}/resolve", headers=env.h["dispatcher"], json={"status": "received"}).status_code == 403
    done = validate(env, "admin", handover_id, item_ids, ["received"] * 3)
    assert done.status_code == 200 and done.json()["handover"]["status"] == "closed"
    with env.Session() as db:
        roles = {e.role for e in db.execute(select(TripEvent)).scalars().all()}
        assert roles == {"admin"}  # recorded with role admin


def test_duplicate_validate_same_key_replays_and_new_key_is_already_done(env):
    trip_id, handover_id, item_ids = seed_returned_handover(env)
    idem = key()
    first = validate(env, "officer", handover_id, item_ids, ["received"] * 3, idem=idem).json()
    events_after_first = events_of(env)
    again = validate(env, "officer", handover_id, item_ids, ["received"] * 3, idem=idem).json()
    assert again["replayed"] is True and again["handover"]["status"] == "closed" and first["replayed"] is False
    other = validate(env, "officer2", handover_id, item_ids, ["received"] * 3).json()  # a different officer / key: nothing left to do
    assert other["already_done"] is True and other["done_by"]["name"] == "Olivia Officer" and other["replayed"] is False
    assert events_of(env) == events_after_first  # no extra rows
    resolve_done = env.client.post(f"/api/officer/invoices/{item_ids[0]}/resolve", headers=env.h["officer"], json={"idempotency_key": key(), "status": "received"}).json()
    assert resolve_done["already_done"] is True
    assert events_of(env) == events_after_first
    # a closed handover cannot be turned back into missing
    assert validate(env, "officer", handover_id, item_ids, ["missing"] * 3, notes={0: "x", 1: "y", 2: "z"}).status_code == 409


def test_validation_errors_missing_item_and_missing_note(env):
    trip_id, handover_id, item_ids = seed_returned_handover(env)
    # every item must be given a status
    partial = env.client.post(f"/api/officer/handovers/{handover_id}/validate", headers=env.h["officer"], json={"idempotency_key": key(), "items": [{"item_id": item_ids[0], "status": "received"}]})
    assert partial.status_code == 422
    detail = partial.json()["detail"]
    assert detail["code"] == "validation" and {e["key"] for e in detail["errors"]} == {str(item_ids[1]), str(item_ids[2])}
    # missing / discrepancy need a note
    no_note = validate(env, "officer", handover_id, item_ids, ["received", "missing", "discrepancy"], notes={2: "ok"})
    assert no_note.status_code == 422 and [e["key"] for e in no_note.json()["detail"]["errors"]] == [str(item_ids[1])]
    assert validate(env, "officer", handover_id, item_ids, ["received", "lost", "received"]).status_code == 422  # unknown status
    stranger = env.client.post(f"/api/officer/handovers/{handover_id}/validate", headers=env.h["officer"], json={"idempotency_key": key(), "items": [{"item_id": 9999, "status": "received"}]})
    assert stranger.status_code == 422
    with env.Session() as db:
        assert db.get(InvoiceHandover, handover_id).status == "pending"
        assert db.execute(select(InvoiceHandoverItem).where(InvoiceHandoverItem.officer_status != "pending")).first() is None  # nothing written
    assert events_of(env) == []
    bad = env.client.post(f"/api/officer/invoices/{item_ids[0]}/resolve", headers=env.h["officer"], json={"status": "missing"})
    assert bad.status_code == 422


def test_history_search_by_so_driver_and_date(env):
    day = t.today() - timedelta(days=3)
    seed_returned_handover(env, "batch-old", day=day, driver=10, helpers=())
    trip_id, handover_id, item_ids = seed_returned_handover(env, "batch-new")
    h = env.h["officer"]
    # validated handovers only
    assert env.client.get("/api/officer/history", headers=h).json() == []
    old_h = env.client.get("/api/officer/handovers?status=all", headers=h).json()
    assert len(old_h) == 2
    for row in old_h:
        detail = env.client.get(f"/api/officer/handovers/{row['handover_id']}", headers=h).json()
        validate(env, "officer", row["handover_id"], [i["item_id"] for i in detail["items"]], ["received"] * 3)
    everything = env.client.get("/api/officer/history", headers=h).json()
    assert len(everything) == 2 and len(everything[0]["items"]) == 3
    assert [r["batch_id"] for r in env.client.get("/api/officer/history?q=SO-batch-old-2", headers=h).json()] == ["batch-old"]
    assert [r["batch_id"] for r in env.client.get("/api/officer/history?q=paul", headers=h).json()] == ["batch-old"]  # driver name, any case
    assert [r["batch_id"] for r in env.client.get("/api/officer/history?driver_id=9", headers=h).json()] == ["batch-new"]
    assert [r["batch_id"] for r in env.client.get(f"/api/officer/history?from={day}&to={day}", headers=h).json()] == ["batch-old"]
    assert [r["batch_id"] for r in env.client.get(f"/api/officer/history?q={day.isoformat()}", headers=h).json()] == ["batch-old"]
    assert env.client.get("/api/officer/history?q=nothing-like-this", headers=h).json() == []
    assert env.client.get("/api/officer/history?from=2026-02-01&to=2026-01-01", headers=h).status_code == 422
    assert env.client.get("/api/officer/history?from=junk", headers=h).status_code == 422
    assert env.client.get("/api/officer/handovers?q=%25", headers=h).json() == []  # a literal percent sign is not a wildcard
