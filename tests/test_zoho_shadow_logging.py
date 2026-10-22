from datetime import date

from services import live_sales_order_cache as lc, zoho_usage


def _so(ts="2026-10-22T01:00:00+00:00", qty=1, sub="confirmed"):
    return {
        "salesorder_id": "100", "salesorder_number": "SO26-100",
        "branch_id": "4489499000001322444", "shipment_date": "2026-10-22", "last_modified_time": ts,
        "status": "confirmed", "current_sub_status": sub, "delivery_method": "truck",
        "line_items": [{"item_id": "i1", "quantity": qty}],
        "lock_details": {"is_locked": False},
    }


def test_shadow_counters_and_mismatch_fields(monkeypatch, caplog):
    monkeypatch.setenv("ZOHO_WEBHOOK_SHADOW", "true")
    monkeypatch.setattr(lc, "_wide_bounds", lambda: (date(2026, 10, 21), date(2026, 11, 4)))
    zoho_usage.reset()
    with lc._detail_lock:
        lc._webhook_copies.clear()
        lc._webhook_copies["100"] = (_so()["last_modified_time"], _so())
    lc._shadow_compare(_so(ts="2026-10-22T02:00:00+00:00", qty=2))
    lc._shadow_compare({**_so(), "salesorder_id": "101", "salesorder_number": "SO26-101"})
    lc._shadow_compare(_so())
    assert zoho_usage.snapshot()["shadow_mismatch"] == 1
    assert zoho_usage.snapshot()["shadow_missing"] == 1
    assert zoho_usage.snapshot()["shadow_ok"] == 1
    assert "fields_differing" in caplog.text and "line_items" in caplog.text


def test_shadow_ignores_disallowed_or_outside_window(monkeypatch):
    monkeypatch.setenv("ZOHO_WEBHOOK_SHADOW", "true")
    monkeypatch.setattr(lc, "_wide_bounds", lambda: (date(2026, 10, 21), date(2026, 11, 4)))
    zoho_usage.reset()
    lc._shadow_compare({**_so(), "branch_id": "4489499000044793937", "salesorder_number": "SS SO26-100"})
    lc._shadow_compare({**_so(), "shipment_date": "2026-12-01"})
    counts = zoho_usage.snapshot()
    assert counts["shadow_ok"] == counts["shadow_mismatch"] == counts["shadow_missing"] == 0
