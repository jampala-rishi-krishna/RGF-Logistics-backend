import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services import order_tracker as tracker, order_tracker_facts as facts, order_tracker_ai as ai, trip_events


def test_today_uses_no_db_or_provider(tracker_env, monkeypatch):
    env = tracker_env
    def blocked(*args, **kwargs):
        pytest.fail("Current tracker must never fetch Neon or Zoho")
    from services import zoho_client, day_model
    monkeypatch.setattr(zoho_client, "fetch_sales_order_detail", blocked)
    monkeypatch.setattr(zoho_client, "fetch_invoices", blocked)
    monkeypatch.setattr(day_model, "rows_in_range", blocked)
    card = tracker.get_order(env.user, "SO-100", env.day, env.day, object())
    assert len(card["sections"]) == 10
    assert card["status"] == "Out for delivery"
    assert card["sections"]["location"]["value"]["plate"] == "DCD 8953"
    assert "09171234567" not in json.dumps(card)
    assert card["sections"]["payment"]["value"]["fully_paid"] is True
    assert card["sections"]["documents"]["value"]["client_po_number"] == "PO-001"


@pytest.mark.parametrize("role,expected", [("sales", {"SO-100", "WM-SO-300"}), ("wet_market", {"WM-SO-300"}), ("admin", {"SO-100", "SO-200", "WM-SO-300"}), ("planner", {"SO-100", "SO-200", "WM-SO-300"})])
def test_scope_list(tracker_env, role, expected):
    env = tracker_env; env.user.role = role
    assert {r["number"] for r in tracker.find_orders(env.user, "", env.day, env.day)["orders"]} == expected


@pytest.mark.parametrize("tool", list(ai.MODELS))
def test_tools_enforce_scope(tracker_env, tool):
    env = tracker_env
    args = ({"query": "SO-200", "date_from": None, "date_to": None, "status": None} if tool == "find_orders" else
            {"customer": "Fresh Foods"} if tool == "customer_rules" else
            {"date_from": None, "date_to": None, "filters": {"trip": None, "customer": None, "area": None}} if tool == "buyers_per_sku" else
            {"so_number": "SO-200"})
    try:
        result = ai.execute(tool, args, env.user, env.day, env.day, None)
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        assert "SO-200" not in json.dumps(result)
        assert "09171234567" not in json.dumps(result)


def test_missing_identity_denies_and_driver_denied(tracker_env):
    env = tracker_env
    env.user.full_name = "Shared Sales Login"
    assert not tracker.find_orders(env.user, "", env.day, env.day)["orders"]
    env.user.role = "driver"
    with pytest.raises(HTTPException) as exc:
        tracker.rows_for(env.user, env.day, env.day)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("outcome,status", [("Delivered", "Delivered"), ("Partially delivered", "Partly delivered"), ("Failed", "Not delivered")])
def test_delivery_result_and_receiver_quantities(tracker_env, outcome, status):
    env = tracker_env
    stop = env.snapshot["stops"]["one"]
    stop.update(outcome=outcome, status="checked_out", checkout_at=env.now, reason="Customer refused some")
    stop["answers"] = [{"key": "receiver_name", "value": "Maria"}, {"key": "receiver_position", "value": "Receiver"},
                       {"key": "invoice_dr_number", "value": "000123"}, {"key": "rejected_items", "value": '[{"sku":"FISH-1","quantity":2,"unit":"kg"}]'},
                       {"key": "delivery_photo", "value": 41}]
    env.save()
    card = tracker.get_order(env.user, "SO-100", env.day, env.day)
    assert card["status"] == status
    result = card["sections"]["delivery_result"]["value"]
    assert result["receiver"] == "Maria" and result["invoice_dr"] == "000123"
    assert result["rejected_items"][0]["quantity"] == "2"
    assert card["sections"]["proof"]["value"]["photos"][0]["url"] == "/api/media/41"
    assert card["sections"]["location"]["state"] == "not_recorded"


def test_partial_missing_qty_is_not_zero(tracker_env):
    env = tracker_env
    env.snapshot["stops"]["one"].update(outcome="Partially delivered", status="checked_out", checkout_at=env.now)
    env.save()
    result = tracker.get_order(env.user, "SO-100", env.day, env.day)["sections"]["delivery_result"]["value"]
    assert result["rejected_items"].startswith("Not recorded")
    assert result["receiver"].startswith("Not recorded")


@pytest.mark.parametrize("mode", ["stale", "missing", "invalid", "no_fix"])
def test_unavailable_gps_never_leaks_coordinates(tracker_env, monkeypatch, mode):
    env = tracker_env
    gps = {"lat": 14.6, "lng": 121., "gps_time": env.now}
    if mode == "stale": gps["gps_time"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    if mode == "invalid": gps["lat"] = 999
    if mode == "no_fix": gps["gps_time"] = None
    monkeypatch.setattr(tracker.live_gps_store, "get", lambda plate: None if mode == "missing" else gps)
    location = tracker.get_order(env.user, "SO-100", env.day, env.day)["sections"]["location"]
    assert location["state"] == "not_recorded"
    assert "lat" not in (location["value"] or {})


def test_cold_and_generation_stale_trip(tracker_env, monkeypatch):
    env = tracker_env
    monkeypatch.setattr(trip_events, "generation", lambda: -1)
    card = tracker.get_order(env.user, "SO-100", env.day, env.day)
    assert card["status"] == "Status unavailable"
    assert card["sections"]["status_now"]["state"] == "not_recorded"
    assert "lat" not in (card["sections"]["location"]["value"] or {})


def test_not_loaded_and_to(tracker_env):
    env = tracker_env; env.snapshot["trip"] = {"status": "pending"}; env.snapshot["acks"] = {}; env.save()
    assert tracker.get_order(env.user, "SO-100", env.day, env.day)["status"] == "Not yet loaded"
    env.rows.append(env.make("TO:1", None, "TO-1", "TO"))
    card = tracker.get_order(env.admin, "TO-1", env.day, env.day)
    assert card["order_type"] == "TO"
    assert "TO-1" not in json.dumps(tracker.buyers_per_sku(env.admin, env.day, env.day))


def test_missing_proof_storage_off_and_invoice_discrepancy(tracker_env, monkeypatch):
    env = tracker_env
    card = tracker.get_order(env.user, "SO-100", env.day, env.day)
    assert card["sections"]["proof"]["state"] == "not_recorded"
    env.snapshot["stops"]["one"]["answers"] = [{"key": "delivery_photo", "value": 9}]
    env.snapshot["invoices"]["one"] = {"status": "discrepancy", "note": "Call driver 09171234567"}
    env.save(); monkeypatch.setattr(tracker.media_storage, "is_enabled", lambda: False)
    card = tracker.get_order(env.user, "SO-100", env.day, env.day)
    assert "storage" in card["sections"]["proof"]["message"]
    assert "09171234567" not in json.dumps(card)
    assert "discrepancy" in json.dumps(card["sections"]["loading_check"])


def test_payment_unknown_partial_and_inconsistent(tracker_env):
    row = tracker.project(tracker_env.rows[0])
    row["invoices"] = []
    assert tracker.payment(row)["value"]["status"] == "Unknown"
    row["invoices"] = [{"total": 100, "balance": 40, "payment_made": 60}]
    assert tracker.payment(row)["value"]["status"] == "Partly paid"
    row["invoices"][0]["payment_made"] = 90
    assert tracker.payment(row)["value"]["arithmetic"] == "doesn't match"
    for invalid in [None, "NaN", "Infinity", -1]:
        row["invoices"][0]["total"] = invalid
        assert tracker.payment(row)["state"] == "not_recorded"


def test_buyers_are_distinct_per_unit_and_scope(tracker_env):
    env = tracker_env
    env.rows.append(env.make("three", "Ana Cruz", "SO-301"))
    result = tracker.buyers_per_sku(env.user, env.day, env.day, {"area": "Bulacan"})
    assert result["items"][0]["buyers"] == 1
    env.rows[-1].raw_json["customer_id"] = "buyer-2"
    assert tracker.buyers_per_sku(env.user, env.day, env.day)["items"][0]["buyers"] == 2


def test_repeated_cached_payment_po_budget_blocked(tracker_env, monkeypatch):
    from services import zoho_client
    monkeypatch.setattr(zoho_client, "_request", lambda *a, **k: pytest.fail("Budget blocked: no provider calls allowed"))
    def read(_): return tracker.get_order(tracker_env.user, "SO-100", tracker_env.day, tracker_env.day)
    with ThreadPoolExecutor(max_workers=8) as pool:
        cards = list(pool.map(read, range(20)))
    assert all(c["sections"]["payment"]["as_of"] for c in cards)
    assert all(c["sections"]["documents"]["value"]["client_po_number"] == "PO-001" for c in cards)


def test_cache_caps_expiry_and_uncommitted_reads(tracker_env, monkeypatch):
    cache = facts.ByteCache(80, 5, 2)
    cache.put("one", {"value": "a" * 20}); cache.put("two", {"value": "b" * 20}); cache.put("three", {"value": "c" * 20})
    assert cache.size <= 80 and len(cache.items) <= 2
    assert cache.put("huge", {"v": "x" * 100}) is False
    monkeypatch.setattr(facts.time, "monotonic", lambda: 10**12)
    assert cache.get("three") is None


def test_projection_is_published_only_by_successful_get(tracker_env):
    db = SimpleNamespace(info={"order_tracker_reads": {"test": tracker_env.snapshot}})
    request = SimpleNamespace(method="POST")
    dependency = facts.publish_existing_reads(request, db); next(dependency)
    with pytest.raises(StopIteration): next(dependency)
    assert facts.facts.get("test") is None
    request.method = "GET"
    dependency = facts.publish_existing_reads(request, db); next(dependency)
    with pytest.raises(StopIteration): next(dependency)
    assert facts.facts.get("test")


def test_historical_cache_single_flight(tracker_env, monkeypatch):
    env = tracker_env; calls = []
    row = env.make(); row.expected_shipment_date = env.day - timedelta(days=1)
    monkeypatch.setattr(tracker.day_model, "rows_in_range", lambda *a: calls.append(1) or [row])
    from services import trip_state
    monkeypatch.setattr(trip_state, "load", lambda *a, **k: None)
    db = SimpleNamespace(info={}, execute=lambda *a: SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])))
    with ThreadPoolExecutor(max_workers=5) as pool:
        result = list(pool.map(lambda _: tracker.historical_rows(db, row.expected_shipment_date, row.expected_shipment_date), range(10)))
    assert len(calls) == 1
    assert result[0][0]["salesperson"] == "Ana Cruz"


def test_direct_photo_scope(tracker_env, monkeypatch):
    from routers import media
    env = tracker_env
    fake_photo = SimpleNamespace(id=41, so_id="two", batch_id="batch-1")
    db = SimpleNamespace(get=lambda *a: fake_photo)
    monkeypatch.setattr(media, "live_sales_order_for_photo", lambda p: tracker.project(env.rows[1]))
    with pytest.raises(HTTPException) as exc:
        media.get_media(41, "full", None, env.user, db)
    assert exc.value.status_code == 404


def test_existing_manifest_and_customer_rule_evidence(tracker_env):
    env = tracker_env
    env.rows[0].manifest_id = 42
    facts.capture_loading(42, {"cargo_count_verified": True, "cargo_count_expected": 4, "cargo_count_actual": 4, "checklist_completed": True})
    facts.capture_rules("Fresh Foods", {"opening_time": "08:00", "receiving_cutoff_time": "16:00", "notes": "Report at gate"})
    card = tracker.get_order(env.user, "SO-100", env.day, env.day)
    assert card["sections"]["loading_check"]["value"]["warehouse_loading"]["cargo_count_verified"] is True
    assert card["sections"]["customer_rules"]["value"]["recorded_rules"]["receiving_cutoff_time"] == "16:00"
