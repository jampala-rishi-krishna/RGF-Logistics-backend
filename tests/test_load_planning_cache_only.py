from datetime import datetime

from services import live_sales_order_cache as cache


def _record(order_id: str) -> dict:
    return {
        "salesorder_id": order_id,
        "salesorder_number": f"SO-{order_id}",
        "branch_id": "4489499000001322444",
        "shipment_date": "2026-10-23",
        "status": "confirmed",
        "last_modified_time": "2026-10-23T05:00:00+00:00",
        "line_items": [{"item_id": "item-1", "quantity": 1, "unit": "case"}],
    }


def test_assigned_snapshot_uses_cached_detail_when_so_detail_cap_blocks(monkeypatch):
    monkeypatch.setattr(cache, "_assignment_state", {
        "so-1": {"assignment_status": "assigned", "expected_shipment_date": datetime(2026, 10, 23).date()}
    })
    monkeypatch.setattr(cache, "_assigned_zoho", cache._OrderRowCache())
    monkeypatch.setattr(cache, "_details", {"so-1": ("2026-10-23T05:00:00+00:00", _record("so-1"))})
    monkeypatch.setattr(cache, "_excluded_ids", {})
    monkeypatch.setattr(cache, "ensure_zoho_data", lambda _oid: (_ for _ in ()).throw(AssertionError("Zoho G4 must not run")))

    rows, had_failures = cache.get_assigned_snapshot_ex(include_window=False, block=True)

    assert not had_failures
    assert [row.id for row in rows] == ["so-1"]


def test_salesorder_webhook_populates_assigned_snapshot_without_g4(monkeypatch):
    monkeypatch.setattr(cache, "_assignment_state", {
        "so-2": {"assignment_status": "assigned", "expected_shipment_date": datetime(2026, 10, 23).date()}
    })
    monkeypatch.setattr(cache, "_assigned_zoho", cache._OrderRowCache())
    monkeypatch.setattr(cache, "_details", {})
    monkeypatch.setattr(cache, "_windows", {})
    monkeypatch.setattr(cache, "_windows_unhydrated", {})
    monkeypatch.setattr(cache, "_excluded_ids", {})
    monkeypatch.setattr(cache, "_persist_live_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cache, "_observe_dates", lambda _records: None)

    assert cache.apply_salesorder_webhook(_record("so-2"))
    rows, had_failures = cache.get_assigned_snapshot_ex(include_window=False, block=True)

    assert not had_failures
    assert [row.id for row in rows] == ["so-2"]
