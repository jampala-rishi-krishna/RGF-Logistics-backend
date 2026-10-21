import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

from services import order_tracker as tracker, order_tracker_ai as ai, order_tracker_facts as facts, trip_state
from tests.order_tracker.test_blocks import ScriptedClient


def test_list_only_confirmed_assigned_selected_day(tracker_env):
    env = tracker_env
    env.rows += [env.make("unassigned", number="SO-U", assignment_status="unassigned"),
                 env.make("draft", number="SO-D", order_status="draft"),
                 env.make("no-truck", number="SO-N", vehicle_id=None),
                 env.make("PO:1", number="PO-1", kind="PO"), env.make("TO:1", number="TO-1", kind="TO"),
                 env.make("tomorrow-unassigned", number="SO-T", expected_shipment_date=env.day + timedelta(days=1), assignment_status="unassigned")]
    result = tracker.find_orders(env.admin, "", env.day, env.day)
    assert {r["number"] for r in result["orders"]} == {"SO-100", "SO-200", "WM-SO-300", "PO-1", "TO-1"}
    assert tracker.find_orders(env.admin, "", env.day + timedelta(days=1), env.day + timedelta(days=1))["orders"] == []


def test_window_orders_are_not_the_assigned_source(tracker_env, monkeypatch):
    monkeypatch.setattr(tracker.live, "cached_assigned_records", lambda: [])
    assert tracker.find_orders(tracker_env.admin, "", tracker_env.day, tracker_env.day)["total"] == 0


def test_tracker_loads_cold_trip_without_opening_other_pages(tracker_env, monkeypatch):
    env = tracker_env
    facts.facts.clear()
    calls = []
    def load(db, batches, names):
        calls.append(batches)
        facts.queue(db, "batch-1", env.snapshot)
    monkeypatch.setattr(trip_state, "load", load)
    db = SimpleNamespace(info={})
    card = tracker.get_order(env.user, "SO-100", env.day, env.day, db)
    assert card["status"] == "Out for delivery" and card["truck"] == "DCD 8953"
    assert card["sections"]["times"]["value"]["left_warehouse"] == env.now
    assert card["sections"]["status_now"]["value"]["stop"] == 1
    assert card["sections"]["status_now"]["value"]["total_stops"] == 1
    assert tracker.find_orders(env.user, "", env.day, env.day, db=db)["counts"]["Out for delivery"] == 3
    assert calls == [["batch-1"]]
    facts.facts.clear()
    tracker.get_order(env.user, "SO-100", env.day, env.day, db)
    assert len(calls) == 2


def test_fresh_gps_uses_cartrack_normalized_plate(tracker_env, monkeypatch):
    env = tracker_env
    env.rows[0].vehicle_id = "DCD 8953"
    seen = []
    def gps(key):
        seen.append(key)
        return {"lat": 14.6, "lng": 121., "gps_time": env.now} if key == "DCD8953" else None
    monkeypatch.setattr(tracker.live_gps_store, "get", gps)
    loc = tracker.get_order(env.user, "SO-100", env.day, env.day)["sections"]["location"]
    assert loc["state"] == "recorded" and loc["value"]["lat"] == 14.6
    assert seen == ["DCD8953"]


def test_ask_english_tomorrow_uses_manila_not_selected_date(tracker_env, monkeypatch):
    env = tracker_env
    monkeypatch.setattr(tracker, "today", lambda: date(2026, 10, 20))
    for row in env.rows:
        row.expected_shipment_date = date(2026, 10, 21)
    client = ScriptedClient([("buyers_per_sku", {"date_from": None, "date_to": None, "filters": {"trip": None, "customer": None, "area": None}})])
    monkeypatch.setattr(ai.openai_client, "_client", lambda: client)
    result = asyncio.run(ai.ask(env.user, "Buyers per SKU tomorrow", date(2026, 10, 20), date(2026, 10, 20)))
    assert result["language"] == "en"
    assert "October 21, 2026" in result["answer"] and "FISH-1" not in result["answer"]
    bars = next(b for b in result["blocks"] if b["type"] == "bars")
    assert bars["items"] == [{"label": "Fish (FISH-1)", "value": 1}]
    assert "Always answer in English" in client.requests[0]["instructions"]
    assert "2026-10-21" in client.requests[0]["input"][0]["content"]


def test_generic_truck_question_uses_scoped_active_orders(tracker_env):
    env = tracker_env
    result = asyncio.run(ai.ask(env.user, "Where is my truck now?", env.day, env.day))
    assert "SO-100" in result["answer"] and "DCD 8953" in result["answer"] and "SO-200" in result["answer"]
    env.snapshot["trip"]["status"] = "pending"; env.save()
    result = asyncio.run(ai.ask(env.user, "Where is my truck now?", env.day, env.day))
    assert result["answer"] == "None of your orders are out for delivery right now."


def test_ask_source_times_are_manila():
    assert ai.display_times("as of 2026-10-20T03:42:38") == "as of 11:42 Manila time"
    assert ai.display_times("as of 2026-10-20T03:42:38Z") == "as of 11:42 Manila time"
