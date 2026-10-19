"""Scoped Order 360. Current dates read existing memory only; no Zoho loaders.

Past-only windows reuse the existing date-scoped history reader behind a byte-
bounded, single-flight cache. No database schema or write path is introduced.
"""
from __future__ import annotations

import json
import math
import re
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from itertools import chain
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from services import day_model, fleet_static_cache, live_gps_store, live_sales_order_cache as live
from services import media_storage, order_tracker_facts as snapshots

MANILA = ZoneInfo("Asia/Manila")
ROLES = {"admin", "planner", "dispatcher", "sales", "wet_market"}
history_cache = snapshots.ByteCache(1024 * 1024, 300, 8)
history_lock = threading.Lock()
PHONE = re.compile(r"(?<!\w)(?:\+?63[\s-]?|0)9(?:[\s-]?\d){9}(?!\w)")


def today():
    return datetime.now(MANILA).date()


def safe_text(value, limit=1200):
    return PHONE.sub("[phone withheld; call Logistics]", str(value))[:limit] if value is not None else None


def norm(value):
    return " ".join(str(value or "").casefold().split())


def allowed(user, row):
    if user.role not in ROLES:
        return False
    if user.role in {"admin", "planner", "dispatcher"}:
        return True
    # No email-prefix guessing or department-wide fallback. Unknown ownership denies.
    owner = norm(row.get("salesperson"))
    if not owner or not norm(user.full_name) or owner != norm(user.full_name):
        return False
    return user.role != "wet_market" or row.get("order_type") == "WM"


def dates(start=None, end=None):
    start, end = start or today(), end or start or today()
    if start > end or (end - start).days > 30:
        raise HTTPException(422, "Choose a date range of at most 31 days, in order.")
    return start, end


def project(row, historical=False):
    oid = str(row.id)
    detail = {} if historical else live.cached_detail(oid) or {}
    raw = {**(getattr(row, "raw_json", None) or {}), **detail}
    number = row.salesorder_number or oid
    kind = str(raw.get("order_type") or getattr(row, "order_type", None) or "SO").upper()
    if str(number).upper().startswith("WM-SO"):
        kind = "WM"
    state = {} if historical else live.get_assignment(oid) or {}
    # Whitelist business fields; never retain the raw provider payload.
    return {"id": oid, "number": number, "customer": safe_text(row.customer_name),
            "customer_id": str(raw.get("customer_id") or norm(row.customer_name)),
            "customer_identity": "id" if raw.get("customer_id") else "name fallback",
            "salesperson": getattr(row, "salesperson_name", None) or raw.get("salesperson_name"),
            "order_type": kind, "date": snapshots.stamp(row.expected_shipment_date),
            "vehicle": state.get("vehicle_id") or getattr(row, "vehicle_id", None),
            "batch": state.get("assignment_batch_id") or getattr(row, "assignment_batch_id", None),
            "assignment_status": state.get("assignment_status") or getattr(row, "assignment_status", None),
            "manifest_id": state.get("manifest_id") or getattr(row, "manifest_id", None),
            "as_of": snapshots.stamp(getattr(row, "synced_at", None) or getattr(row, "updated_at", None)),
            "po_number": safe_text(getattr(row, "customer_po_number", None) or raw.get("customer_po_number") or raw.get("reference_number")),
            "notes": safe_text(raw.get("notes") or getattr(row, "delivery_notes", None)),
            "area": safe_text((raw.get("shipping_address") or {}).get("city")) if isinstance(raw.get("shipping_address"), dict) else None,
            "payment_status": getattr(row, "payment_status", None) or raw.get("payment_status"),
            "invoices": [{k: inv.get(k) for k in ("invoice_id", "invoice_number", "status", "total", "balance", "payment_made", "last_payment_date", "payment_reference")}
                         for inv in (raw.get("invoices") or [])[:10] if isinstance(inv, dict)],
            "lines": [{"sku": safe_text(item.get("sku") or item.get("item_id"), 100), "name": safe_text(item.get("name"), 160),
                       "quantity": item.get("quantity"), "unit": safe_text(item.get("unit"), 40)}
                      for item in (raw.get("line_items") or [])[:200] if isinstance(item, dict)],
            "line_coverage": "truncated" if len(raw.get("line_items") or []) > 200 else "cached",
            "attachment_count": len(raw.get("documents") or raw.get("attachments") or []),
            }


def live_rows(start, end):
    found = {str(r.id): r for r in live.cached_rows_overlapping(start, end)}
    for row in live.cached_assigned_records():
        if row.expected_shipment_date and start <= row.expected_shipment_date <= end:
            found[str(row.id)] = row
    # Stream projections: don't duplicate every order's line items for one request.
    return (project(r) for r in found.values())


def historical_rows(db, start, end):
    key = (str(start), str(end))
    with history_lock:
        cached = history_cache.get(key)
        if cached is not None:
            return cached
        rows = day_model.rows_in_range(db, start, end)
        if len(rows) > 1000:
            raise HTTPException(422, "Too many historical orders. Choose a shorter date range.")
        from services import trip_state
        data = trip_state.load(db, list(day_model.group_by_batch(rows)), names=True)
        snapshots.publish(db)
        from sqlalchemy import select
        from models.sales_order_lines import SalesOrderLine
        lines = db.execute(select(SalesOrderLine).where(SalesOrderLine.sales_order_id.in_([r.id for r in rows]))).scalars().all() if rows else []
        by_order = {}
        for line in lines:
            by_order.setdefault(line.sales_order_id, []).append({"sku": line.sku or line.item_id, "name": safe_text(line.name), "quantity": line.quantity, "unit": line.unit})
        result = []
        for row in rows:
            item = project(row, historical=True)
            item["batch"] = day_model.batch_key(row)
            item["lines"] = by_order.get(row.id, [])[:200]
            item["line_coverage"] = "truncated" if len(by_order.get(row.id, [])) > 200 else "historical snapshot"
            result.append(item)
        # Oversized ranges are not held without a cap.
        if not history_cache.put(key, result):
            raise HTTPException(422, "Historical result is too large. Choose a shorter range.")
        return result


def rows_for(user, start, end, db=None):
    if user.role not in ROLES:
        raise HTTPException(403, "Order Tracker access is not available for this role.")
    start, end = dates(start, end)
    result = live_rows(max(start, today()), end) if end >= today() else []
    if start < today():
        if db is None:
            raise HTTPException(503, "Historical records are unavailable.")
        result = chain(result, historical_rows(db, start, min(end, today() - timedelta(days=1))))
    return (r for r in result if allowed(user, r))


def fact(source, at, value=None, missing=None):
    return {"source": source, "as_of": at, "state": "not_recorded" if missing else "recorded",
            "value": value, "message": missing}


def answer_map(answers):
    return {str(a.get("key")): a.get("value", a.get("text", a.get("checked")))
            for a in answers or [] if isinstance(a, dict)}


def payment(row):
    invoices = row.get("invoices") or []
    at = row["as_of"]
    if len(invoices) != 1:
        return fact("Zoho SO cache / invoices", at, {"status": "Unknown", "so_payment_status": row.get("payment_status")},
                    "Not recorded - an authoritative single invoice total and balance are needed. Ask Logistics to check payment.")
    inv = invoices[0]
    try:
        total, balance = Decimal(str(inv.get("total"))), Decimal(str(inv.get("balance")))
        paid = Decimal(str(inv["payment_made"])) if inv.get("payment_made") is not None else total - balance
        if not all(v.is_finite() and v >= 0 for v in (total, balance, paid)):
            raise ValueError()
    except (InvalidOperation, TypeError, ValueError):
        return fact("Zoho SO cache / invoices", at, {"status": "Unknown"}, "Not recorded - invoice totals / paid amount / balance are incomplete. Ask Logistics.")
    matches = abs(total - paid - balance) <= Decimal("0.01")
    return fact("Zoho SO cache / invoices (not independently verified)", at,
                {"status": "Paid" if balance == 0 else "Partly paid" if paid > 0 else "Unpaid",
                 "invoice_number": safe_text(inv.get("invoice_number")), "invoice_total": str(total), "paid": str(paid), "balance": str(balance),
                 "arithmetic": "matches" if matches else "doesn't match", "fully_paid": matches and paid == total and balance == 0,
                 "paid_basis": "recorded payment_made" if inv.get("payment_made") is not None else "derived from invoice total minus balance; not a payment receipt",
                 "payment_date": safe_text(inv.get("last_payment_date")), "payment_reference": safe_text(inv.get("payment_reference")),
                 "note": "Arithmetic check only; cash receipt and payment reference may not be recorded. Currency not recorded unless shown on the invoice."})


def location(row, snapshot, stop):
    at = snapshot.get("as_of") if snapshot else None
    if (row["date"] != today().isoformat() or row.get("assignment_status") not in {"assigned", "completed"}
            or not snapshot or snapshot["trip"].get("status") != "on_trip" or stop.get("checkout_at")):
        return fact("Cartrack memory / current trip", at, missing="Live location unavailable - only shown while this truck is carrying your order on an active trip.")
    vehicle = next((v for v in fleet_static_cache.vehicles() if str(v["id"]) == str(row.get("vehicle"))), None)
    plate = (vehicle or {}).get("plate_no") or row.get("vehicle")
    gps = live_gps_store.get(str(plate))
    if not gps:
        return fact("Cartrack memory", None, missing="Not recorded - truck GPS is unmapped or unavailable. Call Logistics.")
    fix = live_gps_store.normalize_gps_time(gps.get("gps_time"))
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(fix)).total_seconds()
        fresh = -60 <= age <= 300
    except (TypeError, ValueError):
        fresh = False
    if not fresh:
        return fact("Cartrack GPS fix", fix, {"plate": safe_text(plate), "stale": True}, "Not recorded as a current location - GPS is stale or its fix time is unknown. Call Logistics.")
    lat, lng = gps.get("lat"), gps.get("lng")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)) or not math.isfinite(lat) or not math.isfinite(lng) or not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return fact("Cartrack GPS fix", fix, missing="Not recorded - GPS coordinates are invalid.")
    return fact("Cartrack GPS fix", fix, {"plate": safe_text(plate), "lat": lat, "lng": lng, "stale": False})


def rejected(value):
    try:
        items = json.loads(value) if isinstance(value, str) else value
        if not isinstance(items, list) or len(items) > 100:
            return None
        result = []
        for item in items:
            qty = Decimal(str(item["quantity"]))
            if not qty.is_finite() or qty < 0 or not item.get("sku") or not item.get("unit"):
                return None
            result.append({"sku": safe_text(item["sku"], 100), "quantity": str(qty), "unit": safe_text(item["unit"], 40)})
        return result or None
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return None


def plate_of(row):
    vehicle = next((v for v in fleet_static_cache.vehicles() if str(v["id"]) == str(row.get("vehicle"))), None)
    return safe_text((vehicle or {}).get("plate_no") or row.get("vehicle"))


def order_card(row):
    reader = snapshots.facts.get if row["date"] and row["date"] < today().isoformat() else snapshots.current
    snapshot = reader(row.get("batch")) if row.get("batch") else None
    trip = snapshot["trip"] if snapshot else {}
    stop = snapshot.get("stops", {}).get(row["id"], {}) if snapshot else {}
    ack = snapshot.get("acks", {}).get(row["id"]) if snapshot else None
    invoice = snapshot.get("invoices", {}).get(row["id"]) if snapshot else None
    invoice = {k: safe_text(v) for k, v in invoice.items()} if invoice else None
    at = snapshot.get("as_of") if snapshot else None
    answers = answer_map(stop.get("answers"))
    bucket = day_model.outcome_bucket(stop.get("outcome"))
    status = {"delivered": "Delivered", "partial": "Partly delivered", "failed": "Not delivered"}.get(bucket)
    if not status:
        status = ("Back at warehouse" if trip.get("status") in {"returned", "handover_submitted", "closed"} else
                  "Arrived" if stop.get("checkin_at") else "Out for delivery" if trip.get("status") == "on_trip" else
                  "Loaded" if ack else "Not yet loaded" if snapshot else "Status unavailable")
    photos = []
    for key, value in answers.items():
        if key.endswith("photo") and str(value).isdigit():
            photos.append({"photo_id": int(value), "url": f"/api/media/{int(value)}?read_only=true", "order": row["number"],
                           "captured_at": stop.get("checkout_at"), "timestamp_basis": "checkout time; capture metadata may differ", "result": safe_text(stop.get("outcome"))})
    proof = fact("trip_stops.form_answers / authenticated media", at, {"photos": photos, "officer_invoice": invoice},
                 None if photos else "Not recorded - no proof photo is available in the current read snapshot. Ask Logistics.")
    if photos and not media_storage.is_enabled():
        proof["message"] = "Photo records exist, but storage is not configured or available. Contact Logistics."
    checked = {k: safe_text(v) for k, v in answer_map((ack or {}).get("answers")).items() if not re.search("phone|mobile|contact|signature|photo", k, re.I)}
    received = safe_text(answers.get("receiver_name"))
    loading = snapshots.facts.get("loading|" + str(row.get("manifest_id"))) if row.get("manifest_id") else None
    rules = snapshots.facts.get("rules|" + norm(row["customer"]))
    issues_snapshot = snapshots.current("issues|" + row["id"])
    issues = issues_snapshot.get("issues") if issues_snapshot else None
    sections = {
        "status_now": fact("trip workflow read snapshot", at, {"status": status, "truck": plate_of(row),
                             "stop": stop.get("sequence"), "total_stops": len(snapshot.get("stops", {})) if snapshot else None},
                            None if snapshot else "Not recorded in current memory - ask Logistics to check the trip in Driver Overview."),
        "location": location(row, snapshot, stop),
        "times": fact("so_acknowledgements / trips / trip_stops", at,
                      {"acknowledged_at": (ack or {}).get("at"), "acknowledged_by": safe_text(((ack or {}).get("by") or {}).get("name")),
                       "left_warehouse": trip.get("started_at"), "arrived": stop.get("checkin_at"),
                       "delivered_at": stop.get("checkout_at") if bucket == "delivered" else None,
                       "checkout_at": stop.get("checkout_at"), "eta": "Not recorded - no calibrated ETA is available."},
                      None if snapshot else "Not recorded in current memory. Ask Logistics to check recorded times."),
        "delivery_result": fact("trip_stops / form_answers", at,
                                {"outcome": safe_text(stop.get("outcome")), "reason": safe_text(stop.get("reason")),
                                 "receiver": received or "Not recorded - add receiver_name to the delivery form.",
                                 "receiver_position": safe_text(answers.get("receiver_position")),
                                 "rejected_items": rejected(answers.get("rejected_items")) or "Not recorded - item quantities were not captured.",
                                 "invoice_dr": safe_text(answers.get("invoice_dr_number"))},
                                None if stop.get("checkout_at") else "Not recorded - the delivery result has not been recorded in this snapshot."),
        "proof": proof,
        "payment": payment(row),
        "documents": fact("Zoho SO cache / reference_number", row["as_of"],
                          {"client_po_number": row.get("po_number") or "Not recorded", "cached_attachment_count": row["attachment_count"],
                           "client_po_copy": "Not recorded - attachments have not been verified as the client PO copy.",
                           "permits": "Not recorded - Logistics must confirm the required documents and their availability."}),
        "customer_rules": fact("Zoho SO cache / notes (unverified instructions)", row["as_of"], {"notes": row.get("notes")},
                               None if row.get("notes") else "Not recorded - ask Logistics to record customer receiving hours and delivery rules."),
        "loading_check": fact("so_acknowledgements / invoice_handover_items", at,
                              {"crew_acknowledgement": {"at": (ack or {}).get("at"), "by": safe_text(((ack or {}).get("by") or {}).get("name"))},
                               "checklist": checked or "Not recorded - acknowledgement checklist may be off.",
                               "officer_invoice": invoice or "Not recorded", "warehouse_loading": "Not recorded in Tracker memory - ask Logistics to check the manifest loading checklist.",
                               "note": "Crew acknowledgement and invoice receipt do not establish correct physical loading."}),
        "problems": fact("driver_issues / Logistics reply (existing read snapshot)", issues_snapshot.get("as_of") if issues_snapshot else None,
                         issues, None if issues else "Not recorded in current memory - ask Logistics to check reported issues."),
    }
    if loading:
        sections["loading_check"]["value"]["warehouse_loading"] = loading["checklist"]
        sections["loading_check"]["value"]["warehouse_loading_as_of"] = loading["as_of"]
        sections["loading_check"]["source"] += " / warehouse_loading_checklists (existing read snapshot)"
    if rules and rules.get("rules"):
        sections["customer_rules"] = fact("client_delivery_constraints (existing assignment-options read)", rules["as_of"],
                                         {"recorded_rules": rules["rules"], "so_notes": row.get("notes")})
    return {"id": row["id"], "number": row["number"], "customer": row["customer"], "delivery_date": row["date"],
            "order_type": row["order_type"], "status": status, "truck": plate_of(row),
            "last_update": at or row["as_of"], "sections": sections,
            "link": "/app/order-tracker?order=" + quote(row["number"], safe="") + "&from=" + str(row["date"]) + "&to=" + str(row["date"]),
            "contact": "Call Logistics"}


def get_order(user, number, start, end, db=None):
    row = next((r for r in rows_for(user, start, end, db) if norm(r["number"]) == norm(number) or r["id"] == number), None)
    if row is None:
        raise HTTPException(404, "Order not found in your accessible cached records.")
    return order_card(row)


def find_orders(user, query, start, end, status=None, db=None, offset=0):
    result = []
    for row in rows_for(user, start, end, db):
        needle = norm(query)
        card = order_card(row)
        if needle and needle not in norm(" ".join(str(v or "") for v in [row["number"], row["customer"], row.get("po_number"), card["truck"], card["sections"]["delivery_result"]["value"]["invoice_dr"], *[i.get("invoice_number") for i in row["invoices"]]])):
            continue
        if status and status != "all":
            if status == "problems" and card["status"] not in {"Partly delivered", "Not delivered"} and not card["sections"]["problems"]["value"]:
                continue
            if status != "problems" and card["status"] != status:
                continue
        result.append({k: card[k] for k in ("number", "customer", "delivery_date", "order_type", "status", "truck", "last_update", "link")} |
                      {"payment": card["sections"]["payment"]["value"]["status"]})
    result.sort(key=lambda r: (r["delivery_date"] or "", r["number"]), reverse=True)
    return {"orders": result[offset:offset + 50], "total": len(result), "offset": offset,
            "source": "Zoho cached orders / trip workflow read snapshots", "as_of": min((r["last_update"] for r in result if r["last_update"]), default=None),
            "coverage": "Accessible cached records only; missing or cold records are not proof of no orders.",
            "scope": "All operations orders" if user.role in {"admin", "planner", "dispatcher"} else "Exact salesperson name matches only"}


def buyers_per_sku(user, start, end, filters=None, db=None):
    filters = filters or {}
    groups = {}
    missing = 0
    for row in rows_for(user, start, end, db):
        if row["order_type"] not in {"SO", "WM"} or (filters.get("trip") and row["batch"] != filters["trip"]):
            continue
        if filters.get("customer") and norm(filters["customer"]) not in norm(row["customer"]):
            continue
        if filters.get("area") and norm(filters["area"]) not in norm(row.get("area")):
            if not row.get("area"):
                missing += 1
            continue
        if not row["lines"] or not row["customer_id"] or row["line_coverage"] == "truncated":
            missing += 1
        for line in row["lines"]:
            if not line["sku"] or not line["unit"] or not row["customer_id"]:
                missing += 1
                continue
            key = (line["sku"], line["unit"])
            if key not in groups and len(groups) >= 100:
                missing += 1
                continue
            group = groups.setdefault(key, {"buyers": set(), "as_of": row["as_of"], "identity": row["customer_identity"]})
            if row["customer_identity"] != "id":
                group["identity"] = "contains customer-name fallbacks; identities may be incomplete"
            if row["as_of"]:
                group["as_of"] = min(group["as_of"], row["as_of"]) if group["as_of"] else row["as_of"]
            group["buyers"].add(row["customer_id"])
    return {"definition": "Distinct customers per SKU and unit, across accessible cached SO/WM orders; TO excluded.",
            "items": [{"sku": sku, "unit": unit, "buyers": len(g["buyers"]), "source": "Zoho cached SO line_items", "as_of": g["as_of"], "identity": g["identity"]}
                      for (sku, unit), g in sorted(groups.items())][:100],
            "missing_or_incomplete_orders": missing, "coverage": "Cached lines only; not a complete outgoing-orders census.",
            "message": None if groups else "Not recorded - no eligible SKU/customer/unit facts are cached."}
