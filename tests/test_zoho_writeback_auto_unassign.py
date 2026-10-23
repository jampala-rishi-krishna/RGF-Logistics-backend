"""Delivery write-back to Zoho (A1 Closed sub-status, A2 POD attachment) and automatic unassigns (A3 trip end not delivered /
cancelled, A4 Zoho delivery-date change). SQLite + mocked Zoho: no live Zoho call is ever made."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from models.sales_order_history import SalesOrderHistory
from models.trip import Trip, TripEvent, TripPhoto, TripStop
from services import auto_unassign, dispatch_changes, driver_assignments, live_sales_order_cache, trip_timeline, zoho_client, zoho_writeback
from tests.test_trip_workflow import H, add_order, body, checkout_body, env, make_user, start_trip, url  # noqa: F401  (env is a fixture)


@pytest.fixture()
def wb(env, monkeypatch):
    """Background jobs run on demand with the test database; Zoho write calls are recorded (or made to fail)."""
    monkeypatch.setattr(zoho_writeback, "_open_session", env.Session)
    monkeypatch.setattr(auto_unassign, "_open_session", env.Session)
    monkeypatch.setattr(live_sales_order_cache, "_assignment_state", {})
    calls: dict[str, list] = {"close": [], "attach": []}
    failures: dict[str, list] = {"close": [], "attach": []}

    def close(so_id, code):
        if failures["close"]:
            raise failures["close"].pop(0)
        calls["close"].append((so_id, code))
        return {"code": 0}

    def attach(so_id, name, data, content_type="image/jpeg"):
        if failures["attach"]:
            raise failures["attach"].pop(0)
        calls["attach"].append((so_id, name, len(data)))
        return {"code": 0}

    monkeypatch.setattr(zoho_client, "set_sales_order_substatus", close)
    monkeypatch.setattr(zoho_client, "attach_to_sales_order", attach)
    monkeypatch.setenv("ZOHO_SO_CLOSED_SUBSTATUS", "cs_closed")
    env.calls, env.failures = calls, failures
    return env


def _events(env, event_type, so_id=None):
    with env.Session() as db:
        q = select(TripEvent).where(TripEvent.event_type == event_type)
        if so_id:
            q = q.where(TripEvent.so_id == so_id)
        return db.execute(q.order_by(TripEvent.id)).scalars().all()


def _deliver(env, h, so, outcome="Delivered", batch="b1", **answers):
    assert env.client.post(url(f"orders/{so}/check-in", batch), headers=h, json=body()).status_code == 200
    payload = checkout_body(outcome, **answers)
    r = env.client.post(url(f"orders/{so}/check-out", batch), headers=h, json=payload)
    assert r.status_code == 200, r.text
    return payload


# ---- A1: Closed ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["Delivered", "Partially delivered"])
def test_delivered_or_partial_checkout_closes_the_so_exactly_once(wb, outcome):
    add_order(wb, "s1")
    h = H(make_user(wb))
    start_trip(wb, h)
    payload = _deliver(wb, h, "s1", outcome)
    zoho_writeback.run_pending(force=True)
    assert wb.calls["close"] == [("s1", "cs_closed")]
    rows = _events(wb, zoho_writeback.CLOSE_EVENT)
    assert [(r.result["status"], r.idempotency_key, r.role) for r in rows] == [("ok", "zoho-close:s1", "system")]
    # replay of the same check-out, and a second trigger: nothing is sent again
    assert wb.client.post(url("orders/s1/check-out"), headers=h, json=payload).json()["replayed"] is True
    with wb.Session() as db:
        zoho_writeback.after_checkout(db, "b1", "s1")
    zoho_writeback.run_pending(force=True)
    assert wb.calls["close"] == [("s1", "cs_closed")]
    assert len(_events(wb, zoho_writeback.CLOSE_EVENT)) == 1


def test_not_delivered_wm_and_unconfigured_never_close(wb, monkeypatch):
    add_order(wb, "s1")
    add_order(wb, "s2", offset=1)
    with wb.Session() as db:
        db.get(SalesOrderHistory, "s2").salesorder_number = "WM-SO-1"
        db.commit()
    h = H(make_user(wb))
    start_trip(wb, h, sos=("s1", "s2"))
    _deliver(wb, h, "s1", "Failed / Returned")
    _deliver(wb, h, "s2", "Delivered")  # wet market: SO only
    zoho_writeback.run_pending(force=True)
    assert wb.calls["close"] == [] and _events(wb, zoho_writeback.CLOSE_EVENT) == []

    monkeypatch.delenv("ZOHO_SO_CLOSED_SUBSTATUS")
    add_order(wb, "s3", batch="b2")
    start_trip(wb, h, sos=("s3",), batch="b2")
    _deliver(wb, h, "s3", "Delivered", batch="b2")
    zoho_writeback.run_pending(force=True)
    assert wb.calls["close"] == []
    [row] = _events(wb, zoho_writeback.CLOSE_EVENT)
    assert row.result["status"] == "skipped" and "not configured" in row.result["detail"]


def test_zoho_failure_never_blocks_and_is_retried_in_background(wb):
    add_order(wb, "s1")
    h = H(make_user(wb))
    start_trip(wb, h)
    wb.failures["close"].append(zoho_client.ZohoError("Zoho Inventory request timed out."))
    _deliver(wb, h, "s1", "Delivered")  # 200 even though Zoho will fail
    zoho_writeback.run_pending(force=True)
    assert wb.calls["close"] == []
    assert [r.result["status"] for r in _events(wb, zoho_writeback.CLOSE_EVENT)] == ["retrying"]
    assert len(zoho_writeback.pending()) == 1
    zoho_writeback.run_pending(force=True)  # the background retry
    assert wb.calls["close"] == [("s1", "cs_closed")]
    assert [r.result["status"] for r in _events(wb, zoho_writeback.CLOSE_EVENT)] == ["retrying", "ok"]
    assert zoho_writeback.pending() == []


def test_failed_writeback_is_capped_at_four_attempts_and_reloaded(wb):
    add_order(wb, "s1")
    h = H(make_user(wb))
    start_trip(wb, h)
    wb.failures["close"].extend([zoho_client.ZohoError("timeout")] * 10)
    _deliver(wb, h, "s1", "Delivered")
    zoho_writeback.run_pending(force=True)
    assert len(zoho_writeback.pending()) == 1
    zoho_writeback.reset()
    with wb.Session() as db:
        assert zoho_writeback.load_pending(db) == 1
    for _ in range(3):
        zoho_writeback.run_pending(force=True)
    assert zoho_writeback.pending() == []
    assert len(wb.failures["close"]) == 6  # ten failures minus four HTTP attempts
    assert zoho_writeback.RETRY_DELAYS == (60, 300, 900)


def test_zoho_4xx_is_final(wb):
    add_order(wb, "s1")
    h = H(make_user(wb))
    start_trip(wb, h)
    wb.failures["close"].append(zoho_client.ZohoError("Zoho Inventory returned HTTP 400: Invalid sub status."))
    _deliver(wb, h, "s1", "Delivered")
    zoho_writeback.run_pending(force=True)
    assert [r.result["status"] for r in _events(wb, zoho_writeback.CLOSE_EVENT)] == ["failed"]
    assert zoho_writeback.pending() == []


# ---- A2: POD attachment ----------------------------------------------------------------------------------------------------------


def _photo(env, *, so_id="s1", key="up-1", field_key="delivery_photo", storage_key="gdrive:file-1"):
    with env.Session() as db:
        photo = TripPhoto(batch_id="b1", so_id=so_id, field_key=field_key, storage_key=storage_key, content_type="image/jpeg", size_bytes=1234, uploaded_by_user_id=1, upload_key=key)
        db.add(photo)
        db.commit()
        db.refresh(photo)
        return photo


def test_pod_photo_is_attached_once_per_upload_key(wb):
    add_order(wb, "s1")
    photo = _photo(wb)
    with wb.Session() as db:
        zoho_writeback.after_upload(db, photo, file_name="SO-s1_2026-10-21_10-00_delivery.jpg", data=b"\xff\xd8\xff" + b"0" * 100)
        zoho_writeback.after_upload(db, photo, file_name="SO-s1_2026-10-21_10-00_delivery.jpg", data=b"\xff\xd8\xff" + b"0" * 100)  # replay
    zoho_writeback.run_pending(force=True)
    with wb.Session() as db:
        zoho_writeback.after_upload(db, photo, file_name="SO-s1_2026-10-21_10-00_delivery.jpg", data=b"x")  # after success
    zoho_writeback.run_pending(force=True)
    assert wb.calls["attach"] == [("s1", "SO-s1_2026-10-21_10-00_delivery.jpg", 103)]
    [row] = _events(wb, zoho_writeback.ATTACH_EVENT)
    assert row.result["status"] == "ok" and row.idempotency_key == "zoho-attach:up-1" and "data" not in row.payload


def test_attachment_skips_other_photos_and_respects_size_limit(wb, monkeypatch):
    add_order(wb, "s1")
    with wb.Session() as db:
        zoho_writeback.after_upload(db, _photo(wb, key="sig", field_key="signature", storage_key="gdrive:sig"), file_name="a.jpg", data=b"x")
        zoho_writeback.after_upload(db, _photo(wb, key="r2", storage_key="trips/b1/x.jpg"), file_name="a.jpg", data=b"x")
    assert zoho_writeback.pending() == []
    monkeypatch.setenv("ZOHO_ATTACHMENT_MAX_BYTES", "10")
    with wb.Session() as db:
        zoho_writeback.after_upload(db, _photo(wb, key="big", storage_key="gdrive:big"), file_name="big.jpg", data=b"0" * 50)
    zoho_writeback.run_pending(force=True)
    assert wb.calls["attach"] == []
    [row] = _events(wb, zoho_writeback.ATTACH_EVENT)
    assert row.result["status"] == "skipped" and "Google Drive" in row.result["detail"]


def test_attachment_zoho_rejection_keeps_drive_copy_and_is_final(wb):
    add_order(wb, "s1")
    wb.failures["attach"].append(zoho_client.ZohoError("Zoho Inventory returned HTTP 400: Maximum attachments reached."))
    with wb.Session() as db:
        zoho_writeback.after_upload(db, _photo(wb), file_name="a.jpg", data=b"0")
    zoho_writeback.run_pending(force=True)
    assert [r.result["status"] for r in _events(wb, zoho_writeback.ATTACH_EVENT)] == ["failed"]
    assert zoho_writeback.pending() == []
    with wb.Session() as db:
        assert db.execute(select(TripPhoto).where(TripPhoto.upload_key == "up-1")).scalar_one() is not None


# ---- A3 / A4: automatic unassign ----------------------------------------------------------------------------------------------


def _cache_order(monkeypatch, so_id, day, *, batch="b1", status="confirmed", number=None):
    record = {"salesorder_id": so_id, "salesorder_number": number or f"SO-{so_id}", "customer_name": f"Cust {so_id}", "shipment_date": day.isoformat(), "status": status}
    row = live_sales_order_cache._build_transient(record)
    live_sales_order_cache.set_assignment(so_id, vehicle_id="NAA1", driver_id=9, helper_ids=[9], assignment_status="assigned", assignment_batch_id=batch,
                                          expected_shipment_date=day, assigned_at=datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc))
    return row


@pytest.fixture()
def cached(wb, monkeypatch):
    rows: dict = {}
    monkeypatch.setattr(live_sales_order_cache, "find_cached", lambda order_id: rows.get(order_id))
    return rows


def test_not_delivered_stop_is_auto_unassigned_at_trip_end(wb, cached, monkeypatch):
    day = driver_assignments.manila_today()
    for i, so in enumerate(("s1", "s2")):
        add_order(wb, so, offset=i)
        cached[so] = _cache_order(monkeypatch, so, day)
    h = H(make_user(wb))
    start_trip(wb, h, sos=("s1", "s2"))
    _deliver(wb, h, "s1", "Delivered")
    _deliver(wb, h, "s2", "Failed / Returned")
    r = wb.client.post(url("end"), headers=h, json=body())
    assert r.status_code == 200, r.text
    with wb.Session() as db:
        assert db.get(SalesOrderHistory, "s2").assignment_status == "unassigned"
        assert db.get(SalesOrderHistory, "s1").assignment_status == "assigned"  # delivered: never auto-unassigned
        assert db.execute(select(TripStop).where(TripStop.so_id == "s2")).scalar_one().outcome == "Failed / Returned"  # evidence kept
    assert live_sales_order_cache.get_assignment("s2")["assignment_status"] == "unassigned"
    [event] = _events(wb, dispatch_changes.EVENT)
    assert event.so_id == "s2" and event.role == "system"
    assert event.result["automatic"] is True and event.result["message"] == "Auto-unassigned: not delivered (short)"
    assert event.result["previous_state"] == "not_delivered" and event.payload["notify_crew"] is False
    with wb.Session() as db:
        timeline = trip_timeline.build_timeline(db, "b1")
        assert dispatch_changes.driver_changes(db, 9, day, {"b1"}) == []  # no "removed by dispatch" banner after the trip ended
    entry = next(e for e in timeline["events"] if e["type"] == dispatch_changes.EVENT)
    assert entry["message"].startswith("Auto-unassigned: not delivered") and entry["actor_name"] == "System (automatic)"
    assert any(o["so_id"] == "s2" and o["stop"] for o in timeline["orders"])


def test_cancelled_by_request_is_auto_unassigned_and_never_closed(wb, cached, monkeypatch):
    day = driver_assignments.manila_today()
    add_order(wb, "s1")
    cached["s1"] = _cache_order(monkeypatch, "s1", day)
    now = datetime.now(timezone.utc)
    with wb.Session() as db:
        trip = Trip(batch_id="b1", status="handover_submitted", delivery_date=day, driver_staff_id=9, helper_staff_ids=[], started_at=now, ended_at=now)
        db.add(trip)
        db.flush()
        db.add(TripStop(trip_id=trip.id, so_id="s1", sequence=1, so_number="SO-s1", status="checked_out", outcome="cancelled_by_request", reason="customer closed"))
        db.commit()
        zoho_writeback.after_checkout(db, "b1", "s1")
        assert auto_unassign.after_trip_end(db, "b1") == ["s1"]
    zoho_writeback.run_pending(force=True)
    assert wb.calls["close"] == []
    [event] = _events(wb, dispatch_changes.EVENT)
    assert event.result["message"] == "Auto-unassigned: cancelled by request (customer closed)" and event.result["reason"] == "cancelled_by_request"


def test_delivery_date_change_unassigns_and_moves_to_new_date(wb, cached, monkeypatch):
    day = driver_assignments.manila_today()
    new = day + timedelta(days=2)
    add_order(wb, "s1")
    cached["s1"] = _cache_order(monkeypatch, "s1", day)
    h = H(make_user(wb))
    assert wb.client.post(url("orders/s1/acknowledge"), headers=h, json=body(answers=[])).status_code == 200
    # delivered in Zoho, or same date: ignored
    assert auto_unassign.observe_zoho_records([{"salesorder_id": "s1", "shipment_date": day.isoformat()}]) == 0
    assert auto_unassign.observe_zoho_records([{"salesorder_id": "s1", "shipment_date": new.isoformat(), "status": "fulfilled"}]) == 0
    assert auto_unassign.observe_zoho_records([{"salesorder_id": "s1", "shipment_date": new.isoformat(), "status": "confirmed"}]) == 1
    assert auto_unassign.process_date_changes() == ["s1"]
    state = live_sales_order_cache.get_assignment("s1")
    assert state["assignment_status"] == "unassigned" and state["expected_shipment_date"] == new
    shown = live_sales_order_cache._apply_assignment(live_sales_order_cache._build_transient({"salesorder_id": "s1", "shipment_date": new.isoformat()}))
    assert shown.expected_shipment_date == new and shown.assignment_status == "unassigned"  # Inventory, under the new date
    with wb.Session() as db:
        hist = db.get(SalesOrderHistory, "s1")
        assert hist.assignment_status == "unassigned" and hist.expected_shipment_date == new and hist.driver_id is None
        assert dispatch_changes.driver_changes(db, 9, day, set())[0]["kind"] == dispatch_changes.TRIP_REMOVED  # the crew banner still shows
    [event] = _events(wb, dispatch_changes.EVENT)
    assert event.result["message"] == f"Auto-unassigned: delivery date changed from {day.isoformat()} to {new.isoformat()} by Zoho"
    assert event.result["previous_state"] == "acknowledged"
    alerts = auto_unassign.manager_alerts(1)
    assert len(alerts) == 1 and alerts[0]["unread"] and alerts[0]["order_id"] == "s1"
    assert auto_unassign.mark_alert_read(alerts[0]["id"], 1) and not auto_unassign.manager_alerts(1)[0]["unread"]


def test_date_change_never_unassigns_a_delivered_checkout(wb, cached, monkeypatch):
    day = driver_assignments.manila_today()
    add_order(wb, "s1")
    cached["s1"] = _cache_order(monkeypatch, "s1", day)
    h = H(make_user(wb))
    start_trip(wb, h)
    _deliver(wb, h, "s1", "Partially delivered")
    assert auto_unassign.observe_zoho_records([{"salesorder_id": "s1", "shipment_date": (day + timedelta(days=1)).isoformat(), "status": "confirmed"}]) == 1
    assert auto_unassign.process_date_changes() == []
    assert live_sales_order_cache.get_assignment("s1")["assignment_status"] == "assigned"
    assert _events(wb, dispatch_changes.EVENT) == []


def test_sync_hook_survives_errors(monkeypatch):
    monkeypatch.setattr(live_sales_order_cache, "get_assignment", lambda _id: (_ for _ in ()).throw(RuntimeError("boom")))
    assert auto_unassign.observe_zoho_records([{"salesorder_id": "s1", "shipment_date": "2026-10-22"}]) == 0
