"""Post-deploy Fleet/Zoho reconciliation check.

Usage: python backend/scripts/validate_fleet.py SO26-17781 SO26-17782
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from database import SessionLocal
from models.inventory import SalesOrderCache
from routers.fleet import OPERATIONAL_CAPACITY_KG, _vehicle_payload
from services.delivery_status import sales_order_delivery_status
from services.zoho_client import fetch_sales_order_detail
from services.item_weight import calculate_line_weight_kg

DEFAULTS = ["SO26-17781", "SO26-17782", "SO26-17747", "SO26-17677", "SO26-17085"]

def weight(raw):
    total = 0.0
    for item in (raw or {}).get("line_items") or []:
        value = calculate_line_weight_kg(item.get("quantity"), item.get("unit") or item.get("unit_name"), item.get("item_id") or item.get("itemid"), item=item, context="validate_fleet")
        if value is not None: total += value
    return total

def main():
    numbers = sys.argv[1:] or DEFAULTS
    with SessionLocal() as db:
        orders = db.execute(select(SalesOrderCache).where(SalesOrderCache.salesorder_number.in_(numbers))).scalars().all()
        by_vehicle = {}
        for order in orders:
            raw = order.raw_json or {}
            zoho_id = raw.get("salesorder_id") or raw.get("sales_order_id") or raw.get("id")
            fresh = fetch_sales_order_detail(str(zoho_id)) if zoho_id else {}
            fresh = fresh.get("salesorder", fresh) if isinstance(fresh, dict) else {}
            status = sales_order_delivery_status(fresh or raw)
            fields = {k: (fresh or raw).get(k) for k in ("status", "order_status", "shipment_status", "shipping_status", "shipped_status", "sub_status", "current_sub_status")}
            print(f"{order.salesorder_number}: zoho_id={zoho_id} fields={fields} resolved={status} truck={order.vehicle_id}")
            by_vehicle.setdefault(order.vehicle_id, []).append((fresh or raw, status))
        for truck, rows in by_vehicle.items():
            total = sum(weight(raw) for raw, _ in rows)
            delivered = sum(weight(raw) for raw, status in rows if status == "Delivered")
            remaining = max(0.0, total - delivered)
            pct = delivered / total * 100 if total else 0
            print(f"TRUCK {truck}: assigned_kg={remaining:.2f} delivered_kg={delivered:.2f} remaining_kg={remaining:.2f} fulfillment={pct:.1f}%")
            if rows and all(status == "Delivered" for _, status in rows):
                print(f"FLAG: {truck} still has a fully delivered batch")

if __name__ == "__main__": main()
