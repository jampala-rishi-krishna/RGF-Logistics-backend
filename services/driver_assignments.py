"""Driver app data: a driver's assignments, grouped into batches (one batch = one assign click = one truck).

Reads ONLY Neon (sales_orders + sales_order_lines, written once at assignment) plus memory (staff directory cache and,
as a fallback for delivery details, the live SO cache). It never calls Zoho, so it can never spend Zoho budget.
Every query is filtered by the driver id the caller resolved from the JWT; there is no code path that returns another
driver's rows.

"Today" results are kept in memory and re-read from Neon only when the live assignment state changed (generation
counter) or after TTL_SECONDS, so a phone polling every 60 seconds does not hit the database each time.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.sales_order_history import SalesOrderHistory
from models.trip import KIND_CHECKOUT
from services import day_model, delivery_details, form_templates, live_sales_order_cache, staff_directory_cache, stop_order, trip_events, trip_state
from services.pack_units import is_pack_unit
from services.text_repair import repair_deep
from services.sales_order_location import address_lines, address_object, find_city
from services.staff_roles import helper_ids_of

MANILA = ZoneInfo("Asia/Manila")
TTL_SECONDS = 300
MAX_RANGE_DAYS = 366
VISIBLE_STATUSES = day_model.VISIBLE_STATUSES
_EMAIL_PRIORITY = ("failed", "queued", "sent", "skipped")
_STATUS_PRIORITY = ("assigned", "manifested", "completed", "delivered")  # least -> most advanced

_cache_lock = threading.Lock()
_today_cache: dict[tuple[int, str, bool], tuple[float, tuple[int, int], dict]] = {}


def manila_now() -> datetime:
    return datetime.now(MANILA)


def manila_today() -> date:
    return manila_now().date()


# ---- helpers ------------------------------------------------------------------------------------------------


def _num(value) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# One implementation of batch identity / crew membership, shared with Driver Overview (services/day_model.py).
legacy_batch_key = day_model.legacy_batch_key
batch_key = day_model.batch_key
_sort_key = day_model.sort_key
is_crew = day_model.is_crew
_crew_clause = day_model.crew_clause


# ---- queries (all scoped by driver id) ---------------------------------------------------------------------


def rows_for_driver(db: Session, driver_id: int, start: date, end: date) -> list:
    """Rows where `driver_id` (a staff id) is the driver OR one of the helpers."""
    rows = db.execute(
        select(SalesOrderHistory).where(
            _crew_clause(driver_id),
            SalesOrderHistory.expected_shipment_date >= start,
            SalesOrderHistory.expected_shipment_date <= end,
            SalesOrderHistory.assignment_status.in_(VISIBLE_STATUSES),
        )
    ).scalars().all()
    return [row for row in rows if is_crew(row, driver_id)]


def rows_for_batch(db: Session, driver_id: int, batch_id: str) -> list:
    if batch_id.startswith("legacy-"):
        horizon = manila_today()
        rows = rows_for_driver(db, driver_id, horizon - timedelta(days=MAX_RANGE_DAYS + 30), horizon + timedelta(days=30))
        return [row for row in rows if not row.assignment_batch_id and legacy_batch_key(row) == batch_id]
    rows = db.execute(
        select(SalesOrderHistory).where(
            SalesOrderHistory.assignment_batch_id == batch_id,
            _crew_clause(driver_id),
            SalesOrderHistory.assignment_status.in_(VISIBLE_STATUSES),
        )
    ).scalars().all()
    return [row for row in rows if is_crew(row, driver_id)]


_lines_by_order = day_model.lines_by_order


# ---- shaping ------------------------------------------------------------------------------------------------


def _person(staff_id: int) -> dict:
    staff = staff_directory_cache.get_by_id(staff_id) or {}
    return {"id": staff_id, "name": staff.get("name") or f"Staff #{staff_id}", "phone": (staff.get("phone") or None)}


def _order_payload(row, lines: list) -> dict:
    contact, notes = row.delivery_contact, row.delivery_notes
    live = live_sales_order_cache.find_cached(row.id)
    invoice_hint = delivery_details.invoice_hint(live) if live is not None else None
    if contact is None:  # detail was not cached when the order was assigned: use whatever is in memory now (never Zoho)
        if live is not None:
            live_notes, live_contact = delivery_details.from_order(live)
            if live_contact is not None:
                contact = live_contact
                notes = notes if notes is not None else live_notes
    address = address_lines(row.shipping_address)
    shipping = address_object(row.shipping_address)
    totals_ = day_model.order_totals(lines)
    out_lines = [
        {"name": line.name or "-", "quantity": _num(line.quantity), "unit": line.unit, "packs": _num(line.quantity) if is_pack_unit(line.unit) else None, "kg": None if line.weight_kg is None else round(_num(line.weight_kg), 2)}
        for line in lines
    ]
    return repair_deep({
        "id": row.id,
        "so_number": row.salesorder_number or row.id,
        "customer_name": row.customer_name,
        "contact": None if contact is None else {"person": contact.get("person"), "phone": contact.get("phone"), "attention": contact.get("attention")},
        "address_lines": address,
        "address_text": ", ".join(address),
        "area": find_city(shipping),
        "shipment_date": row.expected_shipment_date.isoformat() if row.expected_shipment_date else None,
        "lines": out_lines,
        "total_kg": totals_["kg"],
        "kg_complete": totals_["kg_complete"],
        "kg_unknown_lines": totals_["kg_unknown_lines"],
        "total_packs": totals_["packs"],  # None = no pack-unit line on this order (unknown, shown as "-"), never 0
        "notes": notes,
        "order_status": row.order_status,
        "shipment_status": row.shipment_status,
        "delivery_status": row.delivery_status,
        "details_loading": contact is None,
        "invoice_hint": invoice_hint,  # pre-fill for the check-out Invoice / DR number; null = the crew types it
    })


def _checkout_prefill(template: dict | None, row, address_text: str, crew: list[dict]) -> dict:
    """Server-side values for the checkout form's prefilled items, keyed by item key."""
    if not template:
        return {}
    driver = next((p["name"] for p in crew if p["role"] == "driver"), None)
    helpers = ", ".join(p["name"] for p in crew if p["role"] == "helper") or None
    sources = {
        "so_number": row.salesorder_number or row.id, "customer": row.customer_name, "address": address_text, "driver": driver, "vehicle": row.vehicle_id,
        "helper": helpers, "date": row.expected_shipment_date.isoformat() if row.expected_shipment_date else None,
    }
    return {item["key"]: sources.get(item["prefill"]) for item in template["items"] if item.get("prefill")}


def _batch_payload(rows: list, lines: dict[str, list], driver_id: int, *, with_orders: bool, data: trip_state.TripData, can_act: bool, checkout_template: dict | None) -> dict:
    rows = sorted(rows, key=_sort_key)
    first = rows[0]
    batch_id = batch_key(first)
    trip = data.trips.get(batch_id)
    frozen = data.stops.get(trip.id) if trip else None
    ordered_ids, source = stop_order.order_for_batch(rows)
    if frozen:  # the sequence is frozen once the trip started
        ordered_ids = sorted((row.id for row in rows), key=lambda so_id: (frozen[so_id].sequence if so_id in frozen else 10 ** 6, so_id))
    position = {so_id: index for index, so_id in enumerate(ordered_ids)}
    rows = sorted(rows, key=lambda row: position.get(row.id, 10 ** 6))
    orders = [_order_payload(row, lines.get(row.id, [])) for row in rows]
    crew = trip_state.crew_of(rows)
    trip_summary, facts = trip_state.summarize(batch_id, rows, data, can_act=can_act, order_source_optimized=source == "optimized")
    shared = day_model.facts_for_rows(batch_id, rows, lines, data)  # THE numbers (same function Driver Overview uses)
    helpers = [_person(hid) for hid in helper_ids_of(first.driver_id if first.driver_id is not None else driver_id, first.helper_ids)]
    emails = [row.email_status for row in rows if row.email_status]
    statuses = {row.assignment_status for row in rows}
    batch = {
        "batch_id": batch_id,
        "delivery_date": first.expected_shipment_date.isoformat() if first.expected_shipment_date else None,
        "vehicle": first.vehicle_id,
        "so_count": shared.so_count,
        "total_kg": shared.kg,
        "kg_complete": shared.kg_complete,
        "kg_unknown_lines": shared.kg_unknown_lines,
        "total_packs": shared.packs,  # None = unknown (no pack-unit line), shown as "-"
        "assigned_at": trip_state.iso(first.assigned_at),
        "progress": {"acknowledged": shared.acknowledged, "stops_done": shared.stops_done, "delivered": shared.delivered, "partial": shared.partial, "failed": shared.failed},
        "first_area": orders[0]["area"],
        "last_area": orders[-1]["area"],
        "departure_time": None,  # not recorded anywhere yet
        "helpers": helpers,
        "assignment_status": next((s for s in reversed(_STATUS_PRIORITY) if s in statuses), first.assignment_status),
        "email_status": next((s for s in _EMAIL_PRIORITY if s in emails), None),
        "stop_order_set": source == "optimized" or facts["started"],
        "details_loading": any(order["details_loading"] for order in orders),
        "trip": trip_summary,
        "crew": crew,
    }
    if with_orders:
        so_numbers = {row.id: (row.salesorder_number or row.id) for row in rows}
        for row, order in zip(rows, orders):
            order["workflow"] = trip_state.order_workflow(row, facts, trip_summary, data, can_act=can_act, so_numbers=so_numbers)
            order["checkout_prefill"] = _checkout_prefill(checkout_template, row, order["address_text"], crew)
        batch["orders"] = orders
    return batch


def build_batches(db: Session, rows: list, driver_id: int, *, with_orders: bool = False, can_act: bool = True) -> list[dict]:
    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        grouped[batch_key(row)].append(row)
    lines = _lines_by_order(db, [row.id for row in rows])
    data = trip_state.load(db, list(grouped), names=with_orders)
    template = form_templates.load_active(db, KIND_CHECKOUT) if with_orders else None
    batches = [_batch_payload(group, lines, driver_id, with_orders=with_orders, data=data, can_act=can_act, checkout_template=template) for group in grouped.values()]
    # Two Assign clicks to the same truck on the same day are two real trips: numbered in assignment order, never merged.
    facts_by_batch = {batch_id: day_model.facts_for_rows(batch_id, group, lines, data) for batch_id, group in grouped.items()}
    numbering = day_model.trip_numbers(list(facts_by_batch.values()))
    for batch in batches:
        batch["trip_number"], batch["trips_on_date"] = numbering[batch["batch_id"]]
    batches.sort(key=lambda b: (b["trip_number"], b["batch_id"]))
    return sorted(batches, key=lambda b: b["delivery_date"] or "", reverse=True)  # newest day first, Trip 1 before Trip 2 (stable)


# ---- public API used by the router --------------------------------------------------------------------------


def today_batches(db: Session, driver_id: int, can_act: bool = True) -> list[dict]:
    today = manila_today()
    key = (driver_id, today.isoformat(), can_act)
    # Both counters: the live assignment state AND every committed trip action (never serve a stale workflow state).
    generation = (live_sales_order_cache.assignment_generation(), trip_events.generation())
    with _cache_lock:
        hit = _today_cache.get(key)
        if hit is not None and hit[1] == generation and time.monotonic() - hit[0] < TTL_SECONDS:
            return hit[2]["batches"]
    batches = build_batches(db, rows_for_driver(db, driver_id, today, today), driver_id, can_act=can_act)
    with _cache_lock:
        for stale in [k for k in _today_cache if k[1] != today.isoformat()]:
            del _today_cache[stale]
        _today_cache[key] = (time.monotonic(), generation, {"batches": batches})
    return batches


def history_batches(db: Session, driver_id: int, start: date, end: date, can_act: bool = True) -> list[dict]:
    return build_batches(db, rows_for_driver(db, driver_id, start, end), driver_id, can_act=can_act)


HISTORY_FILTERS = ("all", "completed", "failed")
MAX_PAGE_SIZE = 50
DONE_TRIP_STATUSES = ("returned", "handover_submitted", "closed")


def history_page(db: Session, driver_id: int, start: date, end: date, *, status: str = "all", page: int = 1, page_size: int = 10, can_act: bool = True) -> dict:
    """Server-side filtered + paginated history. Filtering uses the shared batch facts (trip data only); the heavy payload
    (lines, contacts, crew) is built for the requested page alone.  completed = trip returned / handed over / closed;
    failed = at least one SO checked out as failed / returned."""
    rows = rows_for_driver(db, driver_id, start, end)
    grouped = day_model.group_by_batch(rows)
    data = trip_state.load(db, list(grouped), names=False)
    facts = {batch_id: day_model.facts_for_rows(batch_id, group, {}, data) for batch_id, group in grouped.items()}
    if status == "completed":
        facts = {b: f for b, f in facts.items() if f.trip is not None and f.trip.status in DONE_TRIP_STATUSES}
    elif status == "failed":
        facts = {b: f for b, f in facts.items() if f.failed > 0}
    ordered = sorted(facts.values(), key=lambda f: (f.date.isoformat() if f.date else "", f.assigned_at.timestamp() if f.assigned_at else 0.0, f.batch_id), reverse=True)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    page = max(1, page)
    window = ordered[(page - 1) * page_size: page * page_size]
    wanted = {f.batch_id for f in window}
    batches = build_batches(db, [row for row in rows if batch_key(row) in wanted], driver_id, can_act=can_act) if wanted else []
    position = {f.batch_id: index for index, f in enumerate(window)}
    batches.sort(key=lambda b: position.get(b["batch_id"], 10 ** 6))
    return {"from": start.isoformat(), "to": end.isoformat(), "status": status, "page": page, "page_size": page_size, "total": len(ordered), "has_more": page * page_size < len(ordered), "batches": batches}


def day_summary(batches: list[dict], open_issues: int = 0) -> dict:
    """The four KPI tiles of the Today screen, from the SAME per-batch progress the cards show."""
    assigned = sum(b["so_count"] for b in batches)
    completed = sum(b["progress"]["stops_done"] for b in batches)
    attention = sum(b["progress"]["failed"] + b["progress"]["partial"] for b in batches)
    return {"assigned": assigned, "completed": completed, "remaining": max(assigned - completed, 0), "needs_attention": attention + open_issues, "open_issues": open_issues,
            "trips": len(batches)}


def batch_detail(db: Session, driver_id: int, batch_id: str, can_act: bool = True) -> dict | None:
    rows = rows_for_batch(db, driver_id, batch_id)
    if not rows:
        return None
    return build_batches(db, rows, driver_id, with_orders=True, can_act=can_act)[0]


def reset_cache() -> None:
    """Test hook."""
    with _cache_lock:
        _today_cache.clear()
