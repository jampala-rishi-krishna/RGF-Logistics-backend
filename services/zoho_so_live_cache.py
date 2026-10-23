"""Neon persistence for current Zoho SO snapshots.

This table is deliberately separate from sales_orders: current/unassigned SOs must never
become history rows. All functions are injectable/testable and never call Zoho.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select

from database import SessionLocal
from models.zoho_so_live_cache import ZohoSoLiveCache

logger = logging.getLogger("zoho_so_live_cache")


def _dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _date(value) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _tracked(snapshot: dict) -> dict:
    return snapshot


_ROW_FIELDS = {
    "salesorder_id", "sales_order_id", "id", "salesorder_number", "sales_order_number", "reference_number",
    "branch_id", "branch_name", "location_id", "location_name", "date", "order_date", "shipment_date",
    "expected_shipment_date", "status", "order_status", "invoice_status", "invoiced_status", "shipment_status",
    "current_sub_status", "order_sub_status", "delivery_method", "customer_po_number", "mode_of_transport",
    "customer_name", "shipping_address", "billing_address", "total", "last_modified_time", "lock_details",
    "lock_detail", "lock", "quantity_packed", "quantity_shipped", "quantity_delivered", "quantity_invoiced",
}
_LINE_FIELDS = {
    "item_id", "itemid", "item_code", "name", "item_description", "description", "sku", "quantity", "unit",
    "unit_name", "usage_unit", "warehouse_id", "warehouse_name", "location_id", "location_name",
    "quantity_packed", "quantity_shipped", "quantity_delivered",
}


def slim_snapshot(record: dict, *, source: str) -> dict:
    """Keep the live drawer/list contract without persisting the complete Zoho response."""
    snapshot = {key: value for key, value in record.items() if key in _ROW_FIELDS and value not in (None, "", [], {})}
    snapshot["source"] = source
    lines = []
    for item in (record.get("line_items") or []):
        if not isinstance(item, dict):
            continue
        line = {key: value for key, value in item.items()
                if key in _LINE_FIELDS and value not in (None, "", [], {})}
        # Zoho sometimes nests warehouse/location data instead of returning the
        # flattened fields. Preserve the live-cache contract in either shape.
        warehouse = item.get("warehouse") or item.get("warehouse_details")
        if isinstance(warehouse, dict):
            line.setdefault("warehouse_id", warehouse.get("warehouse_id") or warehouse.get("id"))
            line.setdefault("warehouse_name", warehouse.get("warehouse_name") or warehouse.get("name"))
        location = item.get("location") or item.get("location_details")
        if isinstance(location, dict):
            line.setdefault("location_id", location.get("location_id") or location.get("id"))
            line.setdefault("location_name", location.get("location_name") or location.get("name"))
        lines.append({key: value for key, value in line.items() if value not in (None, "", [], {})})
    snapshot["line_items"] = lines
    return snapshot


def upsert_snapshot(record: dict, *, source: str) -> bool:
    """Upsert one snapshot only when its timestamp or tracked payload changed."""
    if not isinstance(record, dict):
        return False
    order_id = str(record.get("salesorder_id") or record.get("sales_order_id") or record.get("id") or "").strip()
    if not order_id:
        return False
    snapshot = slim_snapshot(record, source=source)
    modified = _dt(snapshot.get("last_modified_time"))
    shipment = _date(snapshot.get("shipment_date") or snapshot.get("expected_shipment_date"))
    branch = str(snapshot.get("branch_id") or snapshot.get("location_id") or "") or None
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        existing = db.get(ZohoSoLiveCache, order_id)
        if existing is not None:
            old_modified = _dt(existing.last_modified_time)
            old_snapshot = existing.snapshot or {}
            if modified is not None and old_modified is not None and modified < old_modified:
                return False
            if modified == old_modified and _tracked(old_snapshot) == _tracked(snapshot):
                return False
            if modified is None and old_modified is None and _tracked(old_snapshot) == _tracked(snapshot):
                return False
            # Lock/result webhooks can be partial. Merge them into the prior
            # slim copy so a partial event never erases shipment, branch, lines,
            # or the existing timestamp.
            merged = dict(old_snapshot)
            merged.update(snapshot)
            if "line_items" not in snapshot or not snapshot.get("line_items"):
                merged["line_items"] = old_snapshot.get("line_items", [])
            effective_modified = modified or old_modified
            effective_shipment = shipment or existing.shipment_date or _date(merged.get("shipment_date"))
            effective_branch = branch or existing.branch_id or str(merged.get("branch_id") or merged.get("location_id") or "") or None
            existing.shipment_date = effective_shipment
            existing.branch_id = effective_branch
            existing.last_modified_time = effective_modified
            existing.snapshot = merged
            existing.updated_at = now
        else:
            db.add(ZohoSoLiveCache(
                salesorder_id=order_id,
                shipment_date=shipment,
                branch_id=branch,
                last_modified_time=modified,
                snapshot=snapshot,
                updated_at=now,
            ))
        db.commit()
    return True


def load_window(start: date, end: date, allowed_branches: tuple[str, ...]) -> tuple[list[dict], int]:
    """Load only current-cache rows, returning snapshots and approximate JSON bytes read."""
    with SessionLocal() as db:
        rows = db.execute(select(ZohoSoLiveCache).where(
            ZohoSoLiveCache.shipment_date >= start,
            ZohoSoLiveCache.shipment_date <= end,
            ZohoSoLiveCache.branch_id.in_(allowed_branches),
        )).scalars().all()
        snapshots = [dict(row.snapshot or {}) for row in rows]
    return snapshots, len(json.dumps(snapshots, separators=(",", ":"), default=str).encode("utf-8"))


def max_modified(rows: list[dict]) -> datetime | None:
    values = [_dt(row.get("last_modified_time")) for row in rows]
    return max((value for value in values if value is not None), default=None)


def cleanup(today: date) -> int:
    cutoff = today - timedelta(days=3)
    with SessionLocal() as db:
        result = db.execute(delete(ZohoSoLiveCache).where(ZohoSoLiveCache.shipment_date < cutoff))
        db.commit()
        return int(result.rowcount or 0)
