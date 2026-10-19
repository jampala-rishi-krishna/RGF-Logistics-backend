from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from auth.dependencies import CurrentUser
from services import order_tracker as tracker, order_tracker_facts as facts, order_tracker_ai as ai
from services import live_sales_order_cache as live, trip_events


@pytest.fixture
def tracker_env(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    day = tracker.today()
    def make(oid="one", owner="Ana Cruz", number="SO-100", kind="SO", **changes):
        raw = {"salesperson_name": owner, "customer_id": "buyer-1", "order_type": kind,
               "shipping_address": {"city": "Bulacan"}, "notes": "Receive before 4 PM. Call driver 09171234567.",
               "line_items": [{"sku": "FISH-1", "name": "Fish", "quantity": 4, "unit": "kg"}],
               "invoices": [{"invoice_number": "000123", "total": 100, "balance": 0, "payment_made": 100}]}
        row = SimpleNamespace(id=oid, salesorder_number=number, customer_name="Fresh Foods", salesperson_name=owner,
                              expected_shipment_date=day, vehicle_id="TRK1", assignment_batch_id="batch-1", assignment_status="assigned",
                              synced_at=datetime.now(timezone.utc), raw_json=raw, customer_po_number="PO-001", payment_status="paid")
        for key, value in changes.items():
            setattr(row, key, value)
        return row
    rows = [make(), make("two", "Ben Cruz", "SO-200"), make("wm", "Ana Cruz", "WM-SO-300", "WM")]
    monkeypatch.setattr(live, "cached_rows_overlapping", lambda start, end: [r for r in rows if start <= r.expected_shipment_date <= end])
    monkeypatch.setattr(live, "cached_assigned_records", lambda: [])
    monkeypatch.setattr(live, "cached_detail", lambda oid: None)
    monkeypatch.setattr(live, "get_assignment", lambda oid: {})
    monkeypatch.setattr(tracker.fleet_static_cache, "vehicles", lambda: [{"id": "TRK1", "plate_no": "DCD 8953"}])
    monkeypatch.setattr(tracker.live_gps_store, "get", lambda plate: {"lat": 14.6, "lng": 121.0, "gps_time": now, "last_updated": datetime.now(timezone.utc)})
    monkeypatch.setattr(tracker.media_storage, "is_enabled", lambda: True)
    facts.facts.clear(); tracker.history_cache.clear(); ai._users.clear(); ai._answers.clear()
    snapshot = {"generation": trip_events.generation(), "assignment_generation": live.assignment_generation(), "as_of": now,
                "trip": {"status": "on_trip", "started_at": now, "ended_at": None, "vehicle": "TRK1"},
                "acks": {"one": {"at": now, "by": {"name": "Crew member"}, "gps": {"lat": 14.6}, "answers": [{"key": "cold", "checked": True}]}},
                "stops": {"one": {"sequence": 1, "status": "pending", "checkin_at": None, "checkout_at": None, "outcome": None, "reason": None, "answers": []}},
                "invoices": {}}
    facts.facts.put("batch-1", snapshot)
    user = CurrentUser(5, "ana@example.test", "sales", "Ana Cruz", "active")
    return SimpleNamespace(rows=rows, make=make, snapshot=snapshot, now=now, day=day, user=user,
                           admin=CurrentUser(1, "admin@example.test", "admin", "Admin", "active"),
                           save=lambda: facts.facts.put("batch-1", snapshot))
