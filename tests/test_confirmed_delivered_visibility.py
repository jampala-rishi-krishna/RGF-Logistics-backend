from datetime import date
from types import SimpleNamespace


def test_confirmed_assigned_delivered_order_stays_visible(monkeypatch):
    from routers import load_planning

    today = date(2026, 10, 23)
    row = SimpleNamespace(
        id="SO-DELIVERED", salesorder_number="SO26-DELIVERED", order_type="SO",
        expected_shipment_date=today, assignment_status="assigned", vehicle_id="TRK1",
        customer_name="Customer", reference_number=None, raw_json={
            "salesorder_id": "SO-DELIVERED", "status": "confirmed",
            "packages": [{"status": "DELIVERED"}], "branch_id": "1000000001",
        },
    )
    monkeypatch.setattr(load_planning.live_sales_order_cache, "get_assigned_snapshot_ex", lambda **kwargs: ([row], False))
    rows = load_planning._rows_any_branch(None, today.isoformat(), today.isoformat(), None, None, "assigned", order_types="SO")
    assert [item.id for item in rows] == ["SO-DELIVERED"]
