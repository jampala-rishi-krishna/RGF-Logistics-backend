from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from models.inventory import SalesOrderCache
from models.sales_order_history import SalesOrderHistory
from models.sales_order_lines import SalesOrderLine
from services.delivery_status import sales_order_delivery_status
from services.item_weight import calculate_line_weight_kg
from services.warehouse_stock import stock_for_orders

_MIRRORED_COLUMNS = [
    "salesorder_number", "reference_number", "customer_name", "order_status",
    "invoice_status", "payment_status", "shipment_status", "order_date",
    "expected_shipment_date", "total", "delivery_method", "salesperson_name",
    "customer_po_number", "billing_address", "shipping_address",
    "payment_terms_label", "mode_of_transport", "synced_at",
    "vehicle_id", "driver_id", "route_id", "manifest_id", "assigned_at",
    "assigned_by", "assignment_status", "completed_at",
]

_REEFER_TERMS = ("reefer", "frozen", "chilled", "cold chain", "cold-chain")


def _is_reefer(raw: dict) -> bool:
    text = " ".join(str(raw.get(k) or "") for k in ("notes", "customer_name", "mode_of_transport")).lower()
    return any(term in text for term in _REEFER_TERMS)


def sync_history_row(db: Session, order: SalesOrderCache) -> None:
    """Write-on-event: call this whenever an SO's assignment/status/delivery state actually
    changes (assign, manifest confirm, checklist complete, scheduled Zoho sync detecting a
    delivery-status change) - never on a bulk/unassigned sync. Upserts within the caller's
    existing transaction; the caller still owns db.commit().

    sales_orders is slim (no raw_json, Step 6) - delivery_status/is_reefer are computed here,
    once, from the live order's raw_json (still present on the transient/live-cache object
    passed in), and line items are copied into sales_order_lines."""
    raw = order.raw_json or {}
    history = db.get(SalesOrderHistory, order.id)
    if history is None:
        history = SalesOrderHistory(id=order.id)
        db.add(history)
    for column in _MIRRORED_COLUMNS:
        setattr(history, column, getattr(order, column))
    history.delivery_status = sales_order_delivery_status(raw)
    history.is_reefer = _is_reefer(raw)
    history.helper_ids = getattr(order, "helper_ids", None) or []
    stock = stock_for_orders([order])
    history.mets_qty_available_for_sale = stock.get(f"{order.id}:mets")
    history.glacier_qty_available_for_sale = stock.get(f"{order.id}:glacier")

    db.execute(delete(SalesOrderLine).where(SalesOrderLine.sales_order_id == order.id))
    for item in raw.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        quantity = item.get("quantity")
        unit = item.get("unit") or item.get("unit_name") or item.get("usage_unit")
        nested_item = item.get("item") if isinstance(item.get("item"), dict) else {}
        item_id = item.get("item_id") or item.get("itemid") or nested_item.get("item_id") or nested_item.get("id")
        weight_kg = calculate_line_weight_kg(quantity, unit, item_id, item=item, context=f"SO={order.salesorder_number or order.id}")
        location_obj = item.get("location") if isinstance(item.get("location"), dict) else {}
        warehouse_obj = item.get("warehouse") if isinstance(item.get("warehouse"), dict) else {}
        location_name = item.get("location_name") or item.get("warehouse_name") or location_obj.get("location_name") or warehouse_obj.get("warehouse_name")
        db.add(SalesOrderLine(
            sales_order_id=order.id,
            item_id=str(item_id) if item_id else None,
            name=item.get("name") or item.get("item_description") or item.get("description"),
            sku=item.get("sku") or item.get("item_order") or (str(item_id) if item_id else None),
            quantity=quantity,
            unit=unit,
            quantity_shipped=item.get("quantity_shipped"),
            weight_kg=weight_kg,
            location_name=str(location_name).strip() if location_name else None,
        ))
