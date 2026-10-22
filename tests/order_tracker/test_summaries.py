import asyncio
from copy import deepcopy
from datetime import date, timedelta
from fastapi import HTTPException

import pytest

from services import order_tracker as tracker, order_tracker_ai as ai


@pytest.fixture
def day21(tracker_env, monkeypatch):
    env = tracker_env
    day = date(2026, 10, 21)
    monkeypatch.setattr(tracker, "today", lambda: day + timedelta(days=1))
    from services import staff_directory_cache
    monkeypatch.setattr(staff_directory_cache, "get_by_id", lambda i: {"name": "Driver Juan" if i == 100 else "Helper Ana"})
    monkeypatch.setattr(ai.openai_client, "_client", lambda: pytest.fail("Summary facts must not depend on model arithmetic"))
    env.rows.clear()
    for batch, count, delivered, loaded in [("done", 9, True, True), ("loaded", 4, False, True), ("pending", 8, False, False)]:
        snapshot = deepcopy(env.snapshot)
        snapshot["trip"] = {"status": "closed" if delivered else "pending", "ended_at": env.now if delivered else None}
        snapshot["stops"] = {}
        snapshot["acks"] = {}
        for index in range(count):
            oid = f"{batch}-{index}"
            env.rows.append(env.make(oid, number=f"SO-{oid}", expected_shipment_date=day, assignment_batch_id=batch, driver_id=100, helper_ids=[100, 101]))
            if loaded:
                snapshot["acks"][oid] = {"at": env.now, "answers": []}
            if delivered:
                snapshot["stops"][oid] = {"outcome": "Delivered", "checkout_at": env.now, "answers": []}
        tracker.snapshots.facts.put(batch, snapshot)
    monkeypatch.setattr(tracker, "historical_rows", lambda db, start, end: [tracker.project(r, historical=True) for r in env.rows if start <= r.expected_shipment_date <= end])
    env.day = day
    return env


def test_21_assigned_9_delivered_remaining_12_are_never_reported_as_zero(day21):
    env = day21
    summary = tracker.delivery_summary(env.user, env.day, env.day, "incomplete", object())
    assert summary["total_assigned"] == 21 and summary["delivered"] == 9 and summary["incomplete"] == 12
    assert summary["status_counts"] == {"Delivered": 9, "Loaded": 4, "Not yet loaded": 8}
    assert summary["explicit_not_delivered"] == 0 and summary["pending"] == 12
    assert len(summary["orders"]) == 12 and summary["as_of"]
    for question in ["how many not delivered yesterday?", "pending?", "yesterday i can see only 9 delivered among 21 assigned what was the status of other orders?",
                     "Summary of Orders on 2026-10-21: Total assigned orders: 21 Delivered: 9 Out for delivery: 0 Not delivered: 0"]:
        answer = asyncio.run(ai.ask(env.user, question, env.day, env.day, db=object()))
        assert "21 assigned orders; 9 delivered; 12" in answer["answer"]
        assert answer["date_from"] == answer["date_to"] == "2026-10-21"
        assert answer["tools"] == ["delivery_summary"] and answer["sources"][0]["as_of"]
        assert "Loaded: **4**" in answer["answer"] and "Not yet loaded: **8**" in answer["answer"]


def test_returned_incomplete_orders_show_actual_crew_without_claiming_delivery(day21):
    env = day21
    snapshot = tracker.snapshots.facts.get("loaded")
    snapshot["trip"] = {"status": "returned", "ended_at": env.now}
    tracker.snapshots.facts.put("loaded", snapshot)
    result = asyncio.run(ai.ask(env.user, "give details of the crew people who were not delivered successfully and just landed", env.day, env.day, db=object()))
    assert "**4** have a recorded returned/ended trip" in result["answer"]
    assert "Driver: Driver Juan" in result["answer"] and "Helper: Helper Ana" in result["answer"]
    assert "Back at warehouse" in result["answer"] and "not automatically failed" in result["answer"]
    rows = next(b["rows"] for b in result["blocks"] if b["type"] == "order_table")
    assert len(rows) == 4 and all(r["number"].startswith("SO-loaded") for r in rows)


def test_rejection_is_evidence_based_and_empty_filtered_result_keeps_freshness(day21):
    env = day21
    result = asyncio.run(ai.ask(env.user, "Which orders were rejected yesterday?", env.day, env.day, db=object()))
    assert "21 assigned orders" in result["answer"] and "0 orders with recorded rejection evidence" in result["answer"]
    assert "No matching evidence is not proof" in result["answer"]
    filtered = tracker.find_orders(env.user, "", env.day, env.day, "pending", object())
    assert filtered["total"] == 12 and filtered["counts"]["all"] == 21 and filtered["as_of"]
    with pytest.raises(HTTPException) as exc:
        tracker.status_matches(tracker.order_card(tracker.project(env.rows[0], historical=True)), "invented-status")
    assert exc.value.status_code == 422


def test_summary_counts_all_records_beyond_list_page_and_keeps_empty_uncertain(day21):
    env = day21
    for i in range(50):
        env.rows.append(env.make(f"extra-{i}", number=f"SO-extra-{i}", expected_shipment_date=env.day, assignment_batch_id="pending"))
    result = tracker.delivery_summary(env.user, env.day, env.day, "all", object())
    assert result["total_assigned"] == 71 and sum(result["status_counts"].values()) == 71
    assert tracker.find_orders(env.user, "", env.day, env.day, db=object())["total"] == 71
    assert len(tracker.find_orders(env.user, "", env.day, env.day, db=object())["orders"]) == 50
    env.rows.clear()
    answer = asyncio.run(ai.ask(env.user, "pending?", env.day, env.day, db=object()))
    assert "does not establish that zero orders existed" in answer["answer"]


def test_explicit_failed_delivery_is_not_automatically_a_rejection(day21):
    env = day21
    snapshot = tracker.snapshots.facts.get("loaded")
    snapshot["stops"]["loaded-0"] = {"outcome": "Not delivered", "checkout_at": env.now, "reason": "Truck breakdown", "answers": []}
    tracker.snapshots.facts.put("loaded", snapshot)
    summary = tracker.delivery_summary(env.user, env.day, env.day, "rejected", object())
    assert summary["explicit_not_delivered"] == 1 and summary["matched"] == 0
    snapshot["stops"]["loaded-0"]["reason"] = "Customer rejected the delivery"
    tracker.snapshots.facts.put("loaded", snapshot)
    summary = tracker.delivery_summary(env.user, env.day, env.day, "rejected", object())
    assert summary["matched"] == 1 and summary["orders"][0]["reason"] == "Customer rejected the delivery"
