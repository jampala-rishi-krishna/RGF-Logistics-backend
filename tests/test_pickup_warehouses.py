"""Multi-warehouse SOs: the pickup warehouse of each line comes from the existing line-level location (sales_order_lines.location_name
or the cached Zoho SO line), never a new table. No Zoho calls."""
from __future__ import annotations

from types import SimpleNamespace

from services import driver_assignments, warehouses


def test_pickup_label_and_order():
    assert warehouses.pickup_label("Mets Cold Storage - Carmona") == "METS"
    assert warehouses.pickup_label("GLACIER Megafridge") == "GLACIER"
    assert warehouses.pickup_label("  Other Hub ") == "Other Hub"
    assert warehouses.pickup_label("") is None and warehouses.pickup_label(None) is None
    assert warehouses.pickup_warehouses(["Glacier", None, "Mets", "glacier megafridge", ""]) == ["METS", "GLACIER"]
    assert warehouses.pickup_warehouses(["Glacier"]) == ["GLACIER"]
    assert warehouses.pickup_warehouses([]) == []


def test_line_location_reads_every_zoho_shape():
    assert warehouses.line_location({"location_name": "Mets"}) == "Mets"
    assert warehouses.line_location({"warehouse_name": "Glacier"}) == "Glacier"
    assert warehouses.line_location({"location": {"location_name": "Mets"}}) == "Mets"
    assert warehouses.line_location({"warehouse": {"warehouse_name": "Glacier"}}) == "Glacier"
    assert warehouses.line_location({}) is None and warehouses.line_location(None) is None


def test_driver_order_payload_has_line_warehouse_and_pickup_list(monkeypatch):
    monkeypatch.setattr(driver_assignments.live_sales_order_cache, "find_cached", lambda _id: None)
    row = SimpleNamespace(id="s1", salesorder_number="SO-1", customer_name="Cust", delivery_contact={"person": "A", "phone": "1"}, delivery_notes=None,
                          shipping_address={"address": "1 Main", "city": "Makati"}, expected_shipment_date=None, order_status="confirmed", shipment_status=None,
                          delivery_status=None, order_type=None, source_warehouse=None, destination_warehouse=None)
    line = lambda name, loc: SimpleNamespace(name=name, quantity=2, unit="pcs", weight_kg=1.5, location_name=loc)
    payload = driver_assignments._order_payload(row, [line("Beef", "Glacier Megafridge"), line("Pork", "Mets Logistics"), line("Salt", None)])
    assert [ln["warehouse"] for ln in payload["lines"]] == ["GLACIER", "METS", None]
    assert payload["pickup_warehouses"] == ["METS", "GLACIER"]


def test_load_planning_summary_exposes_pickup_warehouses():
    from datetime import date
    from unittest.mock import patch

    from routers import load_planning

    row = SimpleNamespace(
        id="so-both", salesorder_number="SO26-1", reference_number=None, customer_name="Customer", order_status="confirmed", invoice_status=None, payment_status=None,
        shipment_status=None, order_date=None, expected_shipment_date=date(2026, 10, 22), total=1, delivery_method=None, salesperson_name=None, synced_at=None,
        assignment_status="unassigned", vehicle_id=None, driver_id=None, assigned_at=None, assigned_by=None, assignment_batch_id=None, email_status=None, email_error=None,
        email_sent_at=None, shipping_address=None,
        raw_json={"line_items": [
            {"line_item_id": "a", "item_id": "i1", "name": "Beef", "sku": "B", "quantity": 1, "unit": "kg", "location_name": "Glacier Megafridge"},
            {"line_item_id": "b", "item_id": "i2", "name": "Pork", "sku": "P", "quantity": 1, "unit": "kg", "warehouse": {"warehouse_name": "Mets Cold Storage"}},
        ]},
    )
    with patch.object(load_planning, "row_to_dict", side_effect=lambda obj: vars(obj).copy()), \
            patch.object(load_planning, "cached_item_stock", return_value={}), \
            patch.object(load_planning, "calculate_line_weight_kg", return_value=None):
        summary = load_planning._summary(row, db=None, allow_fetch=False)
    assert [p["warehouse"] for p in summary["products"]] == ["GLACIER", "METS"]
    assert summary["pickup_warehouses"] == ["METS", "GLACIER"]
