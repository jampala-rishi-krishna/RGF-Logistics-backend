"""Regression: Order Tracker list/detail must never 500 on webhook-sourced SOs.

Webhook payloads (and Neon live-cache seeds) arrive through apply_salesorder_webhook
without the API-only extras (invoices[], comments) and with custom_field_hash fields.
No Zoho calls: every provider path is monkeypatched.
"""
import glob
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from auth.dependencies import CurrentUser
from services import branches, live_sales_order_cache as live, order_tracker as tracker, order_tracker_facts as facts

FIXTURES = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "fixtures", "zoho_webhooks", "so_*.json")))
OPEN = [f for f in FIXTURES if "closed" not in f]


def _load(path, day, idx, strip=()):
    data = json.load(open(path, encoding="utf-8"))
    record = dict(data.get("salesorder", data))
    record["salesorder_id"] = f"wh-{idx}"
    record["salesorder_number"] = f"SO-WH-{idx}"
    record["shipment_date"] = day.isoformat()
    record["status"] = "confirmed"
    record["last_modified_time"] = f"2026-10-23T0{idx % 10}:00:00+0800"
    for key in strip:
        record.pop(key, None)
    return record


@pytest.fixture
def webhook_env(monkeypatch):
    monkeypatch.setattr(live, "_persist_live_snapshot", lambda record, source: None)
    monkeypatch.setattr(live, "_observe_dates", lambda records: None)
    monkeypatch.setattr(branches, "is_allowed", lambda record: True)
    monkeypatch.setattr(tracker.fleet_static_cache, "vehicles", lambda: [{"id": "TRK1", "plate_no": "DCD 8953"}])
    monkeypatch.setattr(tracker.live_gps_store, "get", lambda plate: None)
    monkeypatch.setattr(tracker.media_storage, "is_enabled", lambda: True)
    monkeypatch.setattr(live, "_assigned_zoho", {})
    monkeypatch.setattr(live, "_assignment_state", {})
    monkeypatch.setattr(live, "_details", {})
    monkeypatch.setattr(live, "_webhook_copies", {})
    monkeypatch.setattr(live, "_windows", {})
    monkeypatch.setattr(live, "_windows_unhydrated", {})
    facts.facts.clear()
    tracker.history_cache.clear()

    def push(records):
        for record in records:
            oid = record["salesorder_id"]
            live._assignment_state[oid] = {"assignment_status": "assigned", "vehicle_id": "TRK1",
                                          "assignment_batch_id": "batch-wh", "assigned_at": datetime.now(timezone.utc),
                                          "expected_shipment_date": record["shipment_date"]}
            assert live.apply_salesorder_webhook(record) is not False
            row = live._build_transient(record)
            live._assigned_zoho[oid] = live._apply_assignment(row)
            live.apply_salesorder_webhook({**record, "last_modified_time": record["last_modified_time"].replace("+0800", "+0801")})
    return push


ADMIN = CurrentUser(1, "admin@example.test", "admin", "Admin", "active")


@pytest.mark.parametrize("days", [0, 1])
@pytest.mark.parametrize("strip", [(), ("invoices", "comments", "documents", "customer_id", "line_items", "shipping_address")])
def test_find_orders_with_webhook_rows_never_raises(webhook_env, days, strip):
    day = tracker.today() + timedelta(days=days)
    webhook_env([_load(path, day, i, strip) for i, path in enumerate(OPEN)])
    result = tracker.find_orders(ADMIN, "", day, day, None, None, 0)
    assert result["total"] == len(OPEN)
    for order in result["orders"]:
        assert order["payment"] == "Unknown" or order["payment"]
    card = tracker.get_order(ADMIN, "SO-WH-0", day, day, None)
    assert card["sections"]["payment"]["value"]["status"]
