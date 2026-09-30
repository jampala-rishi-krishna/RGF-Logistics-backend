"""One-time, notification-free DISPATCH 4-2026 assignment backfill."""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
from pathlib import Path
import sys

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from database import SessionLocal
from models.vehicle import Vehicle
from services import live_sales_order_cache, staff_directory_cache
from services.sales_order_history_sync import sync_history_row
from services.zoho_client import fetch_sales_orders

def norm(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())

def staff_match(value: str, staff: list[dict]) -> dict | None:
    needle = norm(value)
    if not needle or needle == "no staff": return None
    for row in staff:
        name = norm(row.get("name")); first = name.split(" ", 1)[0] if name else ""
        if needle == name or needle == first or needle in name.split(): return row
    return None

def zoho_by_number(number: str) -> dict | None:
    page = 1
    while True:
        body = fetch_sales_orders(page=page, per_page=200)
        records = body.get("salesorders") or []
        for row in records:
            if norm(row.get("salesorder_number")) == norm(number): return row
        context = body.get("page_context") or {}
        more = context.get("has_more_page") is True or str(context.get("has_more_page")).lower() == "true"
        if not records or not more: return None
        page += 1

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("csv_path")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    target = date.fromisoformat(args.date)
    staff = staff_directory_cache.all_staff()
    with SessionLocal() as db:
        vehicles = {norm(v.plate_no): v for v in db.execute(select(Vehicle)).scalars().all()}
        rows = list(csv.DictReader(Path(args.csv_path).open("r", encoding="utf-8-sig", newline="")))
        plan, skipped = [], []
        seen = set()
        for row in rows:
            if norm(row.get("DATE")) != target.isoformat(): continue
            so = str(row.get("SO#") or row.get("SO") or row.get("SALES ORDER") or "").strip()
            if not so or so in seen: continue
            seen.add(so)
            if so.upper().startswith(("TO-", "PO-")): skipped.append((so, "TO/PO code")); continue
            order = zoho_by_number(so)
            if not order: skipped.append((so, "SO not found in Zoho")); continue
            oid = str(order.get("salesorder_id") or order.get("id"))
            existing = live_sales_order_cache.get_assignment(oid)
            if existing and existing.get("assignment_status") not in (None, "unassigned", "released"): skipped.append((so, "already assigned")); continue
            plate = str(row.get("TRUCK DETAILS") or "").strip(); vehicle = vehicles.get(norm(plate))
            if not vehicle: skipped.append((so, f"unknown truck: {plate}")); continue
            names = [x.strip() for x in str(row.get("DRIVER/HELPER") or "").split("/") if x.strip()]
            if names and names[0].casefold() == "no staff": names = []
            matched = [staff_match(name, staff) for name in names]
            if any(item is None for item in matched): skipped.append((so, f"unknown driver/helper: {names}")); continue
            plan.append((so, oid, vehicle, matched))
        for so, oid, vehicle, matched in plan: print(f"{so} -> {vehicle.plate_no} -> {', '.join(x['name'] for x in matched) or 'NO STAFF'}")
        for item in skipped: print(f"SKIP {item[0]}: {item[1]}")
        if args.dry_run: return 0
        for so, oid, vehicle, matched in plan:
            order = live_sales_order_cache.ensure_zoho_data(oid)
            if not order: continue
            ids = [int(x["id"]) for x in matched]
            stamp = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
            live_sales_order_cache.set_assignment(oid, vehicle_id=vehicle.plate_no, driver_id=ids[0] if ids else None, helper_ids=ids, assignment_status="assigned", assigned_at=stamp, assigned_by=0)
            order.vehicle_id, order.driver_id, order.helper_ids = vehicle.plate_no, ids[0] if ids else None, ids
            order.assignment_status, order.assigned_at, order.assigned_by = "assigned", stamp, 0
            sync_history_row(db, order)
        db.commit()
    return 0

if __name__ == "__main__": raise SystemExit(main())
