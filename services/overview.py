"""Driver Overview read model for admin / dispatcher. Contract: docs/TRIP_WORKFLOW.md section 8 (KPI definitions are normative).

Reads ONLY Neon (sales_orders for batches, trips and their children) plus the in-memory staff directory / users. Never Zoho.
Results are cached 15 s per (endpoint, params, trip_events.generation(), live_sales_order_cache.assignment_generation()) so a 30 s
dashboard poll by several users costs at most one set of queries per generation per 15 s.
"""
from __future__ import annotations

import csv
import io
import os
import re
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from models.trip import ISSUE_PRIORITIES, ISSUE_STATUSES, DriverIssue, FormTemplate, FormTemplateItem, InvoiceHandover, InvoiceHandoverItem, Trip, TripEvent
from services import day_model, driver_issues, live_sales_order_cache, order_types, trip_state, staff_directory_cache, trip_events, warehouses
from services.staff_roles import helper_ids_of, staff_kind
from services.trip_events import fail
from services.trip_timeline import actor_role, aware, iso, minutes_between, staff_name, users_by_id

MANILA = ZoneInfo("Asia/Manila")
CACHE_SECONDS = 15
MAX_RANGE_DAYS = 366
WAREHOUSE_CODES = ("METS", "GLACIER")
STATE_PRIORITY = ("on_trip", "ready", "to_acknowledge", "returned", "handover_pending", "closed")
CHIP_LABELS = {"idle": "Idle", "to_acknowledge": "To acknowledge", "ready": "Ready to start", "returned": "Back - hand over invoices", "handover_pending": "Handover pending", "closed": "Closed"}

_lock = threading.Lock()
_cache: dict[tuple, tuple[float, int, int, object]] = {}


def reset_cache() -> None:
    """Test hook."""
    with _lock:
        _cache.clear()


def cached(name: str, params: tuple, producer):
    key = (name, params)
    gen = (trip_events.generation(), live_sales_order_cache.assignment_generation())
    with _lock:
        hit = _cache.get(key)
        if hit is not None and (hit[1], hit[2]) == gen and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[3]
    value = producer()
    with _lock:
        if len(_cache) > 200:
            _cache.clear()
        _cache[key] = (time.monotonic(), gen[0], gen[1], value)
    return value


def manila_today() -> date:
    return datetime.now(MANILA).date()


# ---- parameters -----------------------------------------------------------------------------------------------------


def parse_range(day: str | None, from_: str | None, to: str | None) -> tuple[date, date]:
    def parse(value: str, label: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError:
            fail(422, f"{label} must be YYYY-MM-DD.", "validation")

    if from_ or to:
        if not (from_ and to):
            fail(422, "Both from and to are required.", "validation")
        start, end = parse(from_, "from"), parse(to, "to")
    elif day:
        start = end = parse(day, "date")
    else:
        start = end = manila_today()
    if start > end:
        fail(422, "from must not be after to.", "validation")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        fail(422, f"The date range can be at most {MAX_RANGE_DAYS} days.", "validation")
    return start, end


def parse_warehouse(value: str | None) -> str | None:
    if not value:
        return None
    code = value.strip().upper()
    if code not in WAREHOUSE_CODES:
        fail(422, "warehouse must be METS or GLACIER.", "validation")
    return code


def stale_minutes_default() -> int:
    from services import app_settings

    return max(1, app_settings.get_int("DRIVER_STALE_MINUTES"))


# ---- loading --------------------------------------------------------------------------------------------------------


def _staff_warehouse(staff_id: int | None) -> str | None:
    staff = staff_directory_cache.get_by_id(staff_id) if staff_id is not None else None
    value = str((staff or {}).get("warehouse") or "").strip().upper()
    return value or None


def _batch_warehouse(batch: dict) -> str | None:
    return _staff_warehouse(batch["driver_id"]) or (str(batch["trip"].warehouse).upper() if batch["trip"] is not None and batch["trip"].warehouse else None)


def _batch_dict(facts: day_model.BatchFacts) -> dict:
    """The shape the rest of this module reads, built from the shared facts (no second counting implementation)."""
    stops = sorted(facts.stops.values(), key=lambda s: s.sequence)
    return {
        "batch_id": facts.batch_id, "date": facts.date, "driver_id": facts.driver_id, "helper_ids": facts.helper_ids, "vehicle": facts.vehicle,
        "sos": [{"so_id": o.so_id, "so_number": o.so_number, "customer_name": o.customer_name, "order_type": order_types.order_type_of(str(o.so_id), o.so_number)} for o in facts.orders],
        "trip": facts.trip, "acks": facts.acks, "stops": stops, "handover": facts.handover, "items": facts.items, "facts": facts,
    }


class Data:
    """Everything the overview needs for a date range: day_model.load() (the same read model the driver app uses)."""

    def __init__(self, db: Session, start: date, end: date):
        self.facts = day_model.load(db, start, end)
        self.batches: dict[str, dict] = {batch_id: _batch_dict(facts) for batch_id, facts in self.facts.items()}

    def filtered(self, warehouse: str | None = None, driver_id: int | None = None) -> list[dict]:
        out = []
        for batch in self.batches.values():
            if driver_id is not None and batch["driver_id"] != driver_id:
                continue
            if warehouse and _batch_warehouse(batch) != warehouse:
                continue
            out.append(batch)
        return sorted(out, key=lambda b: (b["date"], b["batch_id"]))


def batch_state(batch: dict) -> str:
    return batch["facts"].state


def _chip_label(state: str, done: int, total: int) -> str:
    if state == "on_trip":
        return f"On trip · stop {min(done + 1, max(total, 1))} of {total}"
    return CHIP_LABELS.get(state, state)


_outcome_bucket = day_model.outcome_bucket


def _active_idle_drivers(warehouse: str | None, driver_id: int | None) -> list[dict]:
    out = []
    for staff in staff_directory_cache.all_staff():
        if staff.get("id") is None or staff_kind(staff) != "driver" or staff.get("active") is False:
            continue
        if driver_id is not None and staff["id"] != driver_id:
            continue
        if warehouse and str(staff.get("warehouse") or "").strip().upper() != warehouse:
            continue
        out.append(staff)
    return out


# ---- summary --------------------------------------------------------------------------------------------------------


def summary(db: Session, start: date, end: date, warehouse: str | None, driver_id: int | None) -> dict:
    # legacy for Overview v2: the 12 KPIs; v2 uses trips_list() tile_counts / orders_line. Kept for old clients.
    data = Data(db, start, end)
    batches = data.filtered(warehouse, driver_id)
    by_driver = day_model.by_driver([b["facts"] for b in batches])
    done_states = ("returned", "handover_pending", "closed")
    on_trip = sum(1 for bs in by_driver.values() if any(b.state == "on_trip" for b in bs))
    returned = sum(1 for bs in by_driver.values() if all(b.state in done_states for b in bs))
    idle = [s for s in _active_idle_drivers(warehouse, driver_id) if s["id"] not in by_driver]
    totals = day_model.totals([b["facts"] for b in batches])
    sos = {"assigned": totals["sos"], "acknowledged": totals["acknowledged"], "delivered": totals["delivered"], "partial": totals["partial"], "failed": totals["failed"]}
    pending_handover = sum(1 for b in batches if b["trip"] is not None and b["trip"].status in ("returned", "handover_submitted"))
    return {"from": start.isoformat(), "to": end.isoformat(), "generated_at": trip_events.now_utc().isoformat(),
            "kpis": {"drivers": {"active": len(by_driver), "on_trip": on_trip, "returned": returned, "idle": len(idle)}, "sos": sos, "batches": totals["batches"], "kg": totals["kg"], "kg_complete": totals["kg_complete"], "kg_unknown_lines": totals["kg_unknown_lines"], "packs": totals["packs"],
                     "trips_pending_handover": pending_handover, "invoices_outstanding": outstanding_invoices(db, warehouse, driver_id),
                     "issues_open": driver_issues.count_open(db)}}


def outstanding_invoices(db: Session, warehouse: str | None, driver_id: int | None) -> int:
    """Handover items not received, across ALL dates (the date range does not apply)."""
    found = db.execute(select(InvoiceHandoverItem, Trip).join(InvoiceHandover, InvoiceHandover.id == InvoiceHandoverItem.handover_id).join(Trip, Trip.id == InvoiceHandover.trip_id)
                       .where(InvoiceHandoverItem.officer_status != "received")).all()
    count = 0
    for _item, trip in found:
        if driver_id is not None and trip.driver_staff_id != driver_id:
            continue
        if warehouse and (_staff_warehouse(trip.driver_staff_id) or str(trip.warehouse or "").upper()) != warehouse:
            continue
        count += 1
    return count


# ---- board ----------------------------------------------------------------------------------------------------------


EVENT_LABELS = {"acknowledged": "Loaded (acknowledged) {so}", "trip_started": "Started the trip", "checked_in": "Arrived at {so}", "checked_out": "Checked out of {so}", "trip_ended": "Ended the trip",
                "handover_submitted": "Handed over invoices", "officer_acknowledged": "Officer received the invoices", "invoice_validated": "Invoice validated {so}", "invoice_resolved": "Invoice resolved {so}", "trip_closed": "Trip closed",
                "order_unassigned": "{so} removed by dispatch"}


def _event_actor_name(event: TripEvent) -> str | None:
    """Who did it, from the staff id recorded on the action row (memory: staff directory; no extra read)."""
    if event.staff_id is not None:
        return staff_name(event.staff_id)
    return None


def _last_action(event: TripEvent | None, so_numbers: dict) -> dict | None:
    """The latest action of ANY crew member: {"type","at","label","by","by_role"} (by = the acting person, never assumed the driver)."""
    if event is None:
        return None
    label = EVENT_LABELS.get(event.event_type, event.event_type).format(so=so_numbers.get(event.so_id, "") if event.so_id else "").strip()
    return {"type": event.event_type, "at": iso(event.client_ts), "label": label, "by": _event_actor_name(event), "by_role": actor_role(event.role)}


def _helpers_detail(driver_id: int | None, helper_ids) -> list[dict]:
    """CONTRACT: [{"staff_id","name","phone"}] of the assignment's helper set (driver excluded), from the staff directory cache."""
    return trip_state.crew_object(driver_id, helper_ids)["helpers"]


def _batch_label(state: str, facts: day_model.BatchFacts) -> str:
    return _chip_label(state, facts.stops_done, facts.so_count)


def board(db: Session, day: date, warehouse: str | None, status: str | None, driver_id: int | None, stale_minutes: int) -> dict:
    # legacy for Overview v2: one row per PERSON; v2 shows one row per trip (trips_list). Kept for old clients.
    """One row per PERSON. A person on several batches (two Assign clicks to the same truck) is ONE row whose progress is the SUM
    over all of their batches today, so it equals what their own app shows; `batches` lists each trip (Trip 1, Trip 2 ...)."""
    data = Data(db, day, day)
    batches = data.filtered(None, None)  # the warehouse filter applies to each PERSON's own staff record, below
    ids = [b["batch_id"] for b in batches]
    last_event: dict[str, TripEvent] = {}
    last_gps_event: dict[str, TripEvent] = {}
    if ids:
        for event in db.execute(select(TripEvent).where(TripEvent.batch_id.in_(ids)).order_by(TripEvent.client_ts, TripEvent.id)).scalars().all():
            if event.event_type == "order_unassigned" or event.role not in day_model.CREW_ROLES:
                continue  # a dispatcher / officer action, not the crew's last activity (it must not reset "stale"); driver OR helper counts
            last_event[event.batch_id] = event
            if event.gps and event.gps.get("lat") is not None:
                last_gps_event[event.batch_id] = event
    now = trip_events.now_utc()
    per_person: dict[int, list[tuple[str, dict]]] = defaultdict(list)
    for batch in batches:
        for staff_id, role in [(batch["driver_id"], "driver")] + [(h, "helper") for h in batch["helper_ids"]]:
            if staff_id is not None:
                per_person[staff_id].append((role, batch))
    rows = []
    for staff_id, entries in per_person.items():
        facts_list = [batch["facts"] for _, batch in entries]
        ranked = sorted(entries, key=lambda e: (STATE_PRIORITY.index(e[1]["facts"].state), e[1]["batch_id"]))
        role, best = ranked[0]
        best_facts = best["facts"]
        state = best_facts.state
        staff = staff_directory_cache.get_by_id(staff_id) or {}
        numbers = {s["so_id"]: s["so_number"] for s in best["sos"]}
        current = next((s for s in best["stops"] if s.status == "checked_in"), None)
        # last action across all of the person's batches
        latest = max((last_event[b["batch_id"]] for _, b in entries if b["batch_id"] in last_event), key=lambda e: (aware(e.client_ts), e.id), default=None)
        gps_event = max((last_gps_event[b["batch_id"]] for _, b in entries if b["batch_id"] in last_gps_event), key=lambda e: (aware(e.client_ts), e.id), default=None)
        action = None
        if latest is not None:
            so_numbers = {so["so_id"]: so["so_number"] for _, b in entries for so in b["sos"]}
            action = _last_action(latest, so_numbers)
        last_at = aware(latest.client_ts) if latest else (aware(best["trip"].started_at) if best["trip"] is not None else None)
        total = day_model.totals(facts_list)
        numbers_ = day_model.trip_numbers(facts_list)
        trips = []
        for _, b in sorted(entries, key=lambda e: (numbers_[e[1]["batch_id"]][0], e[1]["batch_id"])):
            f = b["facts"]
            trips.append({"batch_id": f.batch_id, "trip_number": numbers_[f.batch_id][0], "trips_on_date": numbers_[f.batch_id][1], "vehicle": f.vehicle, "state": f.state, "state_label": _batch_label(f.state, f), "so_count": f.so_count,
                          "kg": f.kg, "kg_complete": f.kg_complete, "kg_unknown_lines": f.kg_unknown_lines, "packs": f.packs, "acknowledged": f.acknowledged, "stops_done": f.stops_done, "role": role_of(b, staff_id)})
        rows.append({"so_numbers": [so["so_number"] for _, b in entries for so in b["sos"]], "staff_id": staff_id, "name": staff.get("name") or f"Staff #{staff_id}", "role": role, "warehouse": str(staff.get("warehouse") or "").upper() or None, "state": state, "state_label": _batch_label(state, best_facts),
                     "vehicle": ", ".join(dict.fromkeys(str(f.vehicle) for f in facts_list if f.vehicle)) or None, "batch_id": best["batch_id"], "current_stop": best_facts.stops[current.so_id].customer_name if current else None,
                     "progress": {"done": total["stops_done"], "total": total["sos"]}, "acknowledged": total["acknowledged"], "batch_count": len(entries), "kg": total["kg"], "kg_complete": total["kg_complete"], "kg_unknown_lines": total["kg_unknown_lines"], "packs": total["packs"],
                     "trips": trips, "last_action": action, "last_gps_at": iso(gps_event.client_ts) if gps_event else None,
                     "helpers_detail": _helpers_detail(best["driver_id"], best["helper_ids"]),
                     "stale": state == "on_trip" and last_at is not None and (now - last_at) > timedelta(minutes=stale_minutes)})
    busy = set(per_person)
    for staff in _active_idle_drivers(warehouse, None):
        if staff["id"] not in busy:
            rows.append({"so_numbers": [], "staff_id": staff["id"], "name": staff.get("name") or f"Staff #{staff['id']}", "role": "driver", "warehouse": str(staff.get("warehouse") or "").upper() or None, "state": "idle", "state_label": "Idle",
                         "vehicle": None, "batch_id": None, "current_stop": None, "progress": {"done": 0, "total": 0}, "acknowledged": 0, "batch_count": 0, "kg": 0.0, "kg_complete": False, "kg_unknown_lines": 0, "packs": None, "trips": [],
                         "last_action": None, "last_gps_at": None, "stale": False, "helpers_detail": []})
    if warehouse:
        rows = [r for r in rows if r["warehouse"] == warehouse]
    if driver_id is not None:
        rows = [r for r in rows if r["staff_id"] == driver_id]
    if status:
        rows = [r for r in rows if r["state"] == status]
    order = {state: i for i, state in enumerate(STATE_PRIORITY + ("idle",))}
    rows.sort(key=lambda r: (order.get(r["state"], 99), str(r["name"]).lower()))
    return {"stale_minutes": stale_minutes, "rows": rows}


def role_of(batch: dict, staff_id: int) -> str:
    return "driver" if batch["driver_id"] == staff_id else "helper"


# ---- person detail --------------------------------------------------------------------------------------------------


def _trip_summary(batch: dict) -> dict:
    trip = batch["trip"]
    state = batch_state(batch)
    done = sum(1 for s in batch["stops"] if s.status == "checked_out")
    return {"status": trip.status if trip else "pending", "chip": state, "chip_label": _chip_label(state, done, len(batch["sos"])), "acknowledged": len(batch["acks"]), "total": len(batch["sos"]), "stops_done": done,
            "started_at": iso(trip.started_at) if trip else None, "ended_at": iso(trip.ended_at) if trip else None, "duration_minutes": minutes_between(trip.started_at, trip.ended_at) if trip else None,
            "handover_status": batch["handover"].status if batch["handover"] else None}


def driver_detail(db: Session, staff_id: int, start: date, end: date) -> dict:
    # legacy for Overview v2: the old person panel; v2 uses the calendar and search. Kept for old clients.
    staff = staff_directory_cache.get_by_id(staff_id)
    data = Data(db, start, end)
    mine = [b for b in data.filtered() if b["driver_id"] == staff_id or staff_id in b["helper_ids"]]
    if staff is None and not mine:
        fail(404, "Person not found.", "not_found")
    staff = staff or {}
    batches, minutes_at_stop = [], []
    totals = {"batches": len(mine), "sos": 0, "acknowledged": 0, "delivered": 0, "partial": 0, "failed": 0, "trips_completed": 0, "trip_minutes": 0.0, "invoices_outstanding": 0}
    for batch in sorted(mine, key=lambda b: (b["date"], b["batch_id"]), reverse=True):
        summary_ = _trip_summary(batch)
        batches.append({"batch_id": batch["batch_id"], "delivery_date": batch["date"].isoformat(), "vehicle": batch["vehicle"], "so_count": len(batch["sos"]), "role": "driver" if batch["driver_id"] == staff_id else "helper",
                        "driver": {"id": batch["driver_id"], "name": staff_name(batch["driver_id"])}, "helpers": [staff_name(h) for h in batch["helper_ids"] if h != batch["driver_id"]], "trip": summary_})
        totals["sos"] += len(batch["sos"])
        totals["acknowledged"] += len(batch["acks"])
        for stop in batch["stops"]:
            bucket = _outcome_bucket(stop.outcome) if stop.status == "checked_out" else None
            if bucket:
                totals[bucket] += 1
            m = minutes_between(stop.checkin_at, stop.checkout_at)
            if m is not None:
                minutes_at_stop.append(m)
        if summary_["duration_minutes"] is not None:
            totals["trips_completed"] += 1
            totals["trip_minutes"] = round(totals["trip_minutes"] + summary_["duration_minutes"], 1)
        totals["invoices_outstanding"] += sum(1 for i in batch["items"] if i.officer_status != "received")
    totals["avg_minutes_at_stop"] = round(sum(minutes_at_stop) / len(minutes_at_stop), 1) if minutes_at_stop else None
    person = {"id": staff_id, "name": staff.get("name") or f"Staff #{staff_id}", "title": staff.get("title"), "role": staff_kind(staff) if staff else ("driver" if any(b["driver_id"] == staff_id for b in mine) else "helper"),
              "warehouse": str(staff.get("warehouse") or "").upper() or None, "phone": staff.get("phone")}
    return {"person": person, "from": start.isoformat(), "to": end.isoformat(), "batches": batches, "totals": totals}


# ---- map ------------------------------------------------------------------------------------------------------------


def map_points(db: Session, start: date, end: date, warehouse: str | None, driver_id: int | None) -> dict:
    # legacy for Overview v2: Overview v2 has no map. Kept for old clients.
    data = Data(db, start, end)
    points = []

    def add(kind, gps, at, batch, so_number=None, customer=None, outcome=None):
        if gps and gps.get("lat") is not None and gps.get("lng") is not None:
            points.append({"type": kind, "lat": gps["lat"], "lng": gps["lng"], "at": iso(at), "so_number": so_number, "customer_name": customer, "batch_id": batch["batch_id"],
                           "driver_name": staff_name(batch["driver_id"]), "accuracy_m": gps.get("accuracy_m"), "outcome": outcome})

    for batch in data.filtered(warehouse, driver_id):
        trip = batch["trip"]
        if trip is not None:
            add("start", trip.started_gps, trip.started_at, batch)
        for stop in batch["stops"]:
            if stop.lat is not None and stop.lng is not None:  # the planned stop (the address), so check-in / out points can be judged against it
                add("stop", {"lat": stop.lat, "lng": stop.lng}, None, batch, stop.so_number, stop.customer_name)
            add("checkin", stop.checkin_gps, stop.checkin_at, batch, stop.so_number, stop.customer_name)
            add("checkout", stop.checkout_gps, stop.checkout_at, batch, stop.so_number, stop.customer_name, stop.outcome)
        if trip is not None:
            add("end", trip.ended_gps, trip.ended_at, batch)
    points.sort(key=lambda p: (p["at"] or "", p["batch_id"]))
    return {"warehouses": [{"id": w["id"], "name": w["name"], "lat": w["lat"], "lng": w["lng"]} for w in warehouses.list_warehouses()], "points": points}


# ---- CSV export -----------------------------------------------------------------------------------------------------

CSV_COLUMNS = ["date", "batch", "driver", "helpers", "vehicle", "so_number", "customer", "acknowledged_at", "acknowledged_by", "arrived_at", "distance_m", "departed_at", "invoice_dr_number", "outcome", "reason",
               "minutes_at_stop", "trip_started", "trip_ended", "handover_status", "invoice_status", "officer", "validated_at",
               # appended (helpers): who did each crew action and in which role (driver | helper), never assumed to be the driver.
               # done_by / role = who delivered (checked out of) the stop.
               "acknowledged_role", "arrived_by", "arrived_role", "done_by", "role"]


def _local(value: datetime | None) -> str:
    value = aware(value)
    return value.astimezone(MANILA).isoformat(timespec="seconds") if value else ""


def _safe(value) -> str:
    """Stops spreadsheet formula injection: a text cell that would start a formula gets a leading apostrophe."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(db: Session, start: date, end: date, warehouse: str | None, driver_id: int | None, q: str | None = None, tile: str | None = None, stale_minutes: int | None = None) -> bytes:
    """Same columns / encoding as always. With q / tile: only the orders of the trips the Trips list shows for those filters
    (the same row matcher, so the file equals the screen)."""
    if day_model.search_key(q) or tile:
        rows, _busy, data = _scoped_rows(db, start, end, warehouse, driver_id, stale_minutes or stale_minutes_default())
        needle = day_model.search_key(q)
        keep = {r["batch_id"] for r in rows if _row_matches(r, needle) and _row_in_tile(r, tile)}
        batches = [b for b in data.filtered(warehouse, driver_id) if b["batch_id"] in keep]
    else:
        data = Data(db, start, end)
        batches = data.filtered(warehouse, driver_id)
    users = users_by_id(db, [a.by_user_id for b in batches for a in b["acks"].values()] + [b["handover"].officer_acknowledged_by_user_id for b in batches if b["handover"]] + [i.officer_user_id for b in batches for i in b["items"]]
                        + [u for b in batches for s in b["stops"] for u in (s.checkin_by_user_id, s.checkout_by_user_id)])

    def who(user_id, fallback_role=None) -> tuple[str, str]:
        if user_id is None:
            return "", ""
        found = users.get(user_id, {})
        return found.get("name") or f"User #{user_id}", actor_role(found.get("role") or fallback_role) or ""
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for batch in batches:
        trip, handover = batch["trip"], batch["handover"]
        stops = {s.so_id: s for s in batch["stops"]}
        items = {i.so_id: i for i in batch["items"]}
        helpers = ", ".join(staff_name(h) or "" for h in batch["helper_ids"])
        for so in batch["sos"]:
            stop, ack, item = stops.get(so["so_id"]), batch["acks"].get(so["so_id"]), items.get(so["so_id"])
            officer = users.get(item.officer_user_id, {}).get("name") if item and item.officer_user_id is not None else ""
            validated = _local(item.officer_checked_at) if item else ""
            writer.writerow([_safe(v) for v in [
                batch["date"].isoformat(), batch["batch_id"], staff_name(batch["driver_id"]) or "", helpers, batch["vehicle"] or "", so["so_number"], so["customer_name"] or "",
                _local(ack.acknowledged_at) if ack else "", users.get(ack.by_user_id, {}).get("name", "") if ack else "",
                _local(stop.checkin_at) if stop else "", "" if not stop or stop.checkin_distance_m is None else stop.checkin_distance_m, _local(stop.checkout_at) if stop else "",
                (trip_state.invoice_no_of(stop) if stop else "") or "", (stop.outcome if stop else "") or "", (stop.reason if stop else "") or "", "" if not stop or minutes_between(stop.checkin_at, stop.checkout_at) is None else minutes_between(stop.checkin_at, stop.checkout_at),
                _local(trip.started_at) if trip else "", _local(trip.ended_at) if trip else "", handover.status if handover else "", item.officer_status if item else "", officer or "", validated,
                who(ack.by_user_id, ack.by_role)[1] if ack else "", *who(stop.checkin_by_user_id if stop else None), *who(stop.checkout_by_user_id if stop else None)]])
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


# ---- templates ------------------------------------------------------------------------------------------------------

KINDS = ("acknowledge_checklist", "delivery_checkout")
ITEM_TYPES = {"acknowledge_checklist": ("checkbox", "count", "photo", "note"),
              "delivery_checkout": ("text", "textarea", "dropdown", "radio", "number", "numeric_text", "date", "datetime", "photo", "signature", "checkbox")}
PREFILLS = ("so_number", "customer", "address", "driver", "vehicle", "helper", "date")


def _template_payload(template: FormTemplate, items: list[FormTemplateItem], *, only_active_items: bool) -> dict:
    shown = [i for i in items if i.active] if only_active_items else items
    return {"id": template.id, "kind": template.kind, "version": template.version, "name": template.name, "is_draft": template.is_draft, "active": template.active, "source_note": template.source_note,
            "items": [{"id": i.id, "key": i.key, "label": i.label, "item_type": i.item_type, "required": i.required, "options": i.options, "prefill": i.prefill, "show_when": i.show_when, "position": i.position, "active": i.active, "app_only": bool(i.app_only)}
                      for i in sorted(shown, key=lambda x: x.position)]}


def _kind_or_404(kind: str) -> str:
    if kind not in KINDS:
        fail(404, "Unknown template kind.", "not_found")
    return kind


def list_templates(db: Session) -> dict:
    out = {}
    for kind in KINDS:
        templates = db.execute(select(FormTemplate).where(FormTemplate.kind == kind).order_by(FormTemplate.version.desc())).scalars().all()
        active = next((t for t in templates if t.active), None)
        items = list(db.execute(select(FormTemplateItem).where(FormTemplateItem.template_id == active.id)).scalars().all()) if active else []
        out[kind] = {"active": _template_payload(active, items, only_active_items=True) if active else None,
                     "versions": [{"version": t.version, "name": t.name, "is_draft": t.is_draft, "active": t.active, "created_at": iso(t.created_at)} for t in templates]}
    return out


def get_template_version(db: Session, kind: str, version: int) -> dict:
    template = db.execute(select(FormTemplate).where(FormTemplate.kind == _kind_or_404(kind), FormTemplate.version == version)).scalar_one_or_none()
    if template is None:
        fail(404, "Template version not found.", "not_found")
    items = list(db.execute(select(FormTemplateItem).where(FormTemplateItem.template_id == template.id)).scalars().all())
    return _template_payload(template, items, only_active_items=False)


def validate_template_items(kind: str, items: list[dict]) -> list[dict]:
    errors: list[dict] = []
    if not items:
        return [{"key": "items", "message": "A template needs at least one item."}]
    seen: dict[str, int] = {}
    for position, item in enumerate(items):
        key = str(item.get("key") or "").strip()
        where = key or f"#{position + 1}"
        if not key or not all(ch.isalnum() or ch == "_" for ch in key):
            errors.append({"key": where, "message": "Item key is required and may only contain letters, digits and underscores."})
        elif key in seen:
            errors.append({"key": key, "message": "Item keys must be unique."})
        if not str(item.get("label") or "").strip():
            errors.append({"key": where, "message": "Label is required."})
        item_type = item.get("item_type")
        if item_type not in ITEM_TYPES[kind]:
            errors.append({"key": where, "message": f"item_type must be one of {', '.join(ITEM_TYPES[kind])}."})
        options = item.get("options")
        if item_type in ("dropdown", "radio"):
            if not isinstance(options, list) or not options or not all(isinstance(o, str) and o.strip() for o in options) or len(set(options)) != len(options):
                errors.append({"key": where, "message": "A dropdown / radio item needs a list of distinct, non-empty options."})
        elif options not in (None, [], ""):
            errors.append({"key": where, "message": "options are only allowed on dropdown / radio items."})
        prefill = item.get("prefill")
        if prefill not in (None, "") and prefill not in PREFILLS:
            errors.append({"key": where, "message": f"prefill must be one of {', '.join(PREFILLS)}."})
        show_when = item.get("show_when")
        if show_when not in (None, {}):
            if not isinstance(show_when, dict) or not isinstance(show_when.get("in"), list) or not show_when["in"]:
                errors.append({"key": where, "message": 'show_when must look like {"key": "<earlier item key>", "in": [values]}.'})
            elif show_when.get("key") not in seen:
                errors.append({"key": where, "message": "show_when must reference an item that appears earlier in the template."})
        if key and key not in seen:
            seen[key] = position
    outcome = next((i for i in items if i.get("key") == "outcome"), None)
    if kind == "delivery_checkout" and outcome is not None and outcome.get("item_type") not in ("dropdown", "radio"):
        errors.append({"key": "outcome", "message": 'The "outcome" item must be a dropdown or radio item (the trip logic reads it). Switch it off to record every stop as Delivered.'})
    if kind == "delivery_checkout" and (outcome is None or outcome.get("active") is False):
        pass  # no outcome question: every checked-out stop is recorded as "Delivered" (services/trip_workflow.py)
    return errors


def create_template_version(db: Session, kind: str, user_id: int, name: str, items: list[dict]) -> dict:
    """Version N+1, made active, previous version(s) deactivated - ONE transaction."""
    _kind_or_404(kind)
    if not (name or "").strip():
        fail(422, "A template name is required.", "validation", [{"key": "name", "message": "Name is required."}])
    errors = validate_template_items(kind, items)
    if errors:
        fail(422, "The template is not valid.", "validation", errors)
    latest = db.execute(select(FormTemplate.version).where(FormTemplate.kind == kind).order_by(FormTemplate.version.desc())).scalars().first() or 0
    for template in db.execute(select(FormTemplate).where(FormTemplate.kind == kind, FormTemplate.active.is_(True))).scalars().all():
        template.active = False
    created = FormTemplate(kind=kind, version=latest + 1, name=name.strip(), is_draft=False, active=True, created_by_user_id=user_id)
    db.add(created)
    db.flush()
    for position, item in enumerate(items, 1):
        prefill = item.get("prefill") or None
        db.add(FormTemplateItem(template_id=created.id, position=position, key=str(item["key"]).strip(), label=str(item["label"]).strip(), item_type=item["item_type"], required=bool(item.get("required")),
                                options=item.get("options") or None, prefill=prefill, show_when=item.get("show_when") or None, active=item.get("active", True) is not False,
                                app_only=bool(item.get("app_only"))))
    db.commit()
    trip_events.bump()
    return get_template_version(db, kind, created.version)


# ---- trips list -----------------------------------------------------------------------------------------------------------


TILES = ("loading", "on_road", "back", "closed", "attention")
TILE_RANK = {"on_road": 1, "loading": 2, "back": 3, "closed": 4}  # after "attention" (0)


def parse_tile(value: str | None) -> str | None:
    if not value:
        return None
    tile = value.strip().lower()
    if tile not in TILES:
        fail(422, f"tile must be one of {', '.join(TILES)}.", "validation")
    return tile


def _crew(facts: day_model.BatchFacts) -> list[dict]:
    """Driver first, then helpers; a helper never gets a row of their own."""
    return ([trip_state._person(facts.driver_id, "driver")] if facts.driver_id is not None else []) + [trip_state._person(h, "helper") for h in facts.helper_ids]


def _photo_rules(template_items: dict[int, list]) -> dict[int, list[dict]]:
    return {tid: [{"key": i.key, "item_type": i.item_type, "required": bool(i.required), "show_when": i.show_when} for i in items if i.item_type == "photo" and i.required and i.active is not False]
            for tid, items in template_items.items()}


def _batch_extras(db: Session, batches: list[dict]) -> tuple[dict, dict, dict]:
    """What attention flags need besides the trip data, for every batch in scope in 3 batched reads (inside the 15 s cache):
    trip_events of the batches, their OPEN driver_issues, and the required photo items of the check-out templates used."""
    ids = [b["batch_id"] for b in batches]
    events: dict[str, list] = defaultdict(list)
    issues: dict[str, list] = defaultdict(list)
    if ids:
        for event in db.execute(select(TripEvent).where(TripEvent.batch_id.in_(ids))).scalars().all():
            events[event.batch_id].append(event)
        for issue in db.execute(select(DriverIssue).where(DriverIssue.batch_id.in_(ids), DriverIssue.status.in_(driver_issues.OPEN_STATUSES))).scalars().all():
            issues[issue.batch_id].append(issue)
    template_ids = {s.form_template_id for b in batches for s in b["stops"] if s.status == "checked_out" and s.form_template_id}
    items: dict[int, list] = defaultdict(list)
    if template_ids:
        for item in db.execute(select(FormTemplateItem).where(FormTemplateItem.template_id.in_(template_ids), FormTemplateItem.item_type == "photo")).scalars().all():
            items[item.template_id].append(item)
    return events, issues, _photo_rules(items)


def _invoice_nos(facts: day_model.BatchFacts) -> list[str]:
    numbers = [trip_state.invoice_no_of(s) for s in facts.stops.values()] + [i.invoice_no for i in facts.items]
    return list(dict.fromkeys(str(n) for n in numbers if n))


def _trip_row(batch: dict, number: tuple[int, int], now: datetime, stale_minutes: int, events: list, issues: list, rules: dict) -> dict:
    """ONE trip (batch) row: every key the old /trips rows had, plus the v2 fields (CONTRACT)."""
    f = batch["facts"]
    trip = batch["trip"]
    step = day_model.driver_step(f)
    minutes = minutes_between(trip.started_at, trip.ended_at) if trip else None
    return {
        "batch_id": batch["batch_id"], "date": batch["date"].isoformat() if batch["date"] else None, "driver_id": batch["driver_id"], "driver_name": staff_name(batch["driver_id"]),
        "helpers": [staff_name(h) for h in batch["helper_ids"]], "vehicle": batch["vehicle"], "trip_number": number[0], "trips_on_date": number[1], "state": f.state,
        "state_label": _batch_label(f.state, f), "so_count": f.so_count, "kg": f.kg, "kg_complete": f.kg_complete, "kg_unknown_lines": f.kg_unknown_lines, "packs": f.packs,
        "acknowledged": f.acknowledged, "stops_done": f.stops_done, "delivered": f.delivered, "partial": f.partial, "failed": f.failed,
        "started_at": iso(trip.started_at) if trip else None, "ended_at": iso(trip.ended_at) if trip else None,
        "trip_minutes": minutes, "handover_status": batch["handover"].status if batch["handover"] else None,
        # ---- v2 ----
        "warehouse": _batch_warehouse(batch), "step": step, "tile": day_model.tile_of(step), "crew": _crew(f),
        "outcomes": {"delivered": f.delivered, "partial": f.partial, "failed": f.failed}, "invoices": day_model.invoice_counts(f),
        "attention": day_model.attention_flags(f, now, stale_minutes, events=events, open_issues=issues, photo_rules=rules),
        "last_crew_activity_at": iso(day_model.last_crew_activity(events)), "duration_minutes": minutes,
        "so_numbers": [o.so_number for o in f.orders], "invoice_nos": _invoice_nos(f), "customers": list(dict.fromkeys(o.customer_name for o in f.orders if o.customer_name)),
        # ---- helpers (additive) ----
        "helpers_detail": _helpers_detail(batch["driver_id"], batch["helper_ids"]), "crew_detail": trip_state.crew_object(batch["driver_id"], batch["helper_ids"]),
        "last_action": _last_action(_latest_crew_event(events), {o.so_id: o.so_number for o in f.orders}),
    }


def _latest_crew_event(events: list) -> TripEvent | None:
    """Same rule as the stale flag (day_model.last_crew_activity): the newest driver OR helper action."""
    crew = [e for e in events if e.event_type != day_model.UNASSIGNED and e.role in day_model.CREW_ROLES and e.client_ts is not None]
    return max(crew, key=lambda e: (aware(e.client_ts), e.id or 0), default=None)


def _row_matches(row: dict, needle: str) -> bool:
    """The ONE trip search (list, tiles, export): driver / helper names, plate, SO / TO numbers, invoice numbers, customers."""
    return day_model.matches(needle, [row["driver_name"], *row["helpers"], row["vehicle"], *row["so_numbers"], *row["invoice_nos"], *row["customers"]])


def _row_in_tile(row: dict, tile: str | None) -> bool:
    return tile is None or (bool(row["attention"]) if tile == "attention" else row["tile"] == tile)


def _scoped_rows(db: Session, start: date, end: date, warehouse: str | None, driver_id: int | None, stale_minutes: int) -> tuple[list[dict], set[int], Data]:
    """Every trip row of the date range + warehouse (+ legacy driver_id) and the staff ids that are crew on any trip of the range."""
    data = Data(db, start, end)
    busy = {sid for b in data.filtered() for sid in [b["driver_id"], *b["helper_ids"]] if sid is not None}
    batches = data.filtered(warehouse, driver_id)
    numbering: dict[int, dict[str, tuple[int, int]]] = {staff_id: day_model.trip_numbers(items) for staff_id, items in day_model.by_driver([b["facts"] for b in batches]).items()}
    events, issues, rules = _batch_extras(db, batches)
    now = trip_events.now_utc()
    rows = [_trip_row(b, numbering.get(b["driver_id"], {}).get(b["batch_id"], (1, 1)), now, stale_minutes, events.get(b["batch_id"], []), issues.get(b["batch_id"], []), rules) for b in batches]
    return rows, busy, data


def trips_list(db: Session, start: date, end: date, warehouse: str | None, driver_id: int | None, q: str | None = None, tile: str | None = None, stale_minutes: int | None = None) -> dict:
    """One row per trip (batch). tile_counts, orders_line, the rows and not_assigned all come from the SAME rows (one code path):
    tile_counts / orders_line after date + warehouse + search, rows after the tile filter as well."""
    stale = stale_minutes or stale_minutes_default()
    rows, busy, _ = _scoped_rows(db, start, end, warehouse, driver_id, stale)
    needle = day_model.search_key(q)
    scope = [r for r in rows if _row_matches(r, needle)]
    counts = {t: 0 for t in TILES}
    for row in scope:
        counts[row["tile"]] += 1
        if row["attention"]:
            counts["attention"] += 1
    orders = sum(r["so_count"] for r in scope)
    outcome = {k: sum(r["outcomes"][k] for r in scope) for k in ("delivered", "partial", "failed")}
    shown = [r for r in scope if _row_in_tile(r, tile)]
    shown.sort(key=lambda r: (-(date.fromisoformat(r["date"]).toordinal() if r["date"] else 0), 0 if r["attention"] else TILE_RANK.get(r["tile"], 9), str(r["vehicle"] or "").casefold(), r["trip_number"], r["batch_id"]))
    not_assigned = [{"staff_id": s["id"], "name": s.get("name") or f"Staff #{s['id']}", "warehouse": str(s.get("warehouse") or "").upper() or None, "phone": s.get("phone") or None}
                    for s in _active_idle_drivers(warehouse, driver_id) if s["id"] not in busy and day_model.matches(needle, [s.get("name")])]
    not_assigned.sort(key=lambda s: str(s["name"]).casefold())
    return {"from": start.isoformat(), "to": end.isoformat(), "generated_at": trip_events.now_utc().isoformat(), "stale_minutes": stale, "tile_counts": counts,
            "orders_line": {"orders": orders, **outcome, "to_go": max(orders - sum(outcome.values()), 0)}, "total": len(shown), "trips": shown, "not_assigned": not_assigned}


# ---- trip drawer ----------------------------------------------------------------------------------------------------------


def _invoice_entry(order: dict) -> dict | None:
    """One line of the drawer's "Invoices for this trip", from the timeline's own order payload (no extra read)."""
    item, stop = order.get("handover"), order.get("stop")
    checkout = (stop or {}).get("checkout") or {}
    if item is not None:
        officer_status = str(item.get("officer_status") or "pending").lower()
        status = ("with_officer" if item.get("handed_over") else "not_handed_over") if officer_status == "pending" else officer_status
        return {"item_id": item.get("item_id"), "handed_over": item.get("handed_over"), "so_id": order["so_id"], "so_number": order["so_number"], "invoice_no": item.get("invoice_no") or checkout.get("invoice_no"), "status": status,
                "officer_name": (item.get("checked_by") or {}).get("name") if item.get("checked_at") else None, "checked_at": item.get("checked_at"), "officer_note": item.get("officer_note")}
    if stop is not None and stop.get("status") == "checked_out":
        return {"so_id": order["so_id"], "so_number": order["so_number"], "invoice_no": checkout.get("invoice_no"), "status": "with_driver", "officer_name": None, "checked_at": None, "officer_note": None}
    return None


def trip_detail(db: Session, batch_id: str, stale_minutes: int | None = None) -> dict:
    """GET /trips/{batch_id}: the full timeline (every existing key) + step, tile, attention, invoices, issues, and per
    order `flags` + `photos`. Reads: the timeline's own reads, the batch's facts (day_model.load_batch), its driver_issues rows and the
    driver's same-day batches for trip numbering (day_model.load with staff_id). All inside the 15 s cache."""
    from services.trip_timeline import build_timeline

    stale = stale_minutes or stale_minutes_default()
    raw: dict = {}
    timeline = build_timeline(db, batch_id, raw)
    if timeline is None:
        fail(404, "Trip not found.", "not_found")
    facts = day_model.load_batch(db, batch_id) or day_model.BatchFacts(batch_id=batch_id, date=None, driver_id=None, helper_ids=[], vehicle=None, orders=[], trip=None, acks={}, stops={}, handover=None)
    issue_rows = _issue_rows(db, status="all", batch_id=batch_id)
    open_rows = [i for i in issue_rows if i.status in driver_issues.OPEN_STATUSES]
    hits = day_model.attention_hits(facts, trip_events.now_utc(), stale, events=raw.get("events", []), open_issues=open_rows, photo_rules=_photo_rules(raw.get("template_items", {})))
    per_stop = day_model.flags_by_stop(hits)
    stops = {s.so_id: s for s in raw.get("stops", [])}
    acks = raw.get("acks", {})
    for order in timeline["orders"]:
        so_id, number = order["so_id"], order["so_number"]
        order["flags"] = per_stop.get(so_id, [])
        order["photos"] = day_model.stop_photos(stops.get(so_id), number) + day_model.ack_photos(acks.get(so_id), number) + [p for i in issue_rows if i.so_id == so_id for p in day_model.issue_photos(i)]
    step = day_model.driver_step(facts)
    seen: dict[str, dict] = {}
    for entry in hits:
        seen.setdefault(entry["code"], entry)
    timeline.update({
        "step": step, "tile": day_model.tile_of(step), "attention": list(seen.values()), "last_crew_activity_at": iso(day_model.last_crew_activity(raw.get("events", []))),
        "invoices": [e for e in (_invoice_entry(o) for o in timeline["orders"]) if e is not None],
        "issues": _issue_payloads(db, issue_rows),
    })
    driver_id = facts.driver_id if facts.driver_id is not None else next((c["id"] for c in timeline["crew"] if c["role"] == "driver"), None)
    day = facts.date or (date.fromisoformat(timeline["batch"]["delivery_date"]) if timeline["batch"].get("delivery_date") else None)
    number = (1, 1)
    if driver_id is not None and day is not None:
        mine = [f for f in day_model.load(db, day, day, staff_id=driver_id).values() if f.driver_id == driver_id]
        numbers = day_model.trip_numbers(mine + ([facts] if facts.orders and all(f.batch_id != batch_id for f in mine) else []))
        number = numbers.get(batch_id, number)
    day_model.photo_capture_times(db, [p for o in timeline["orders"] for p in o["photos"]])
    timeline["batch"].update({"trip_number": number[0], "trips_on_date": number[1], "kg": facts.kg, "kg_complete": facts.kg_complete, "kg_unknown_lines": facts.kg_unknown_lines, "packs": facts.packs,
                              "warehouse": timeline["batch"].get("warehouse") or _staff_warehouse(driver_id)})
    return timeline


# ---- issues (dispatch view) -----------------------------------------------------------------------------------------------


def _issue_rows(db: Session, *, status: str | None = "open", priority: str | None = None, day: date | None = None, batch_id: str | None = None, limit: int = 1000) -> list[DriverIssue]:
    """Same filters and order as driver_issues.list_all (open first, urgent first, newest first) + optional batch / Manila day."""
    query = select(DriverIssue)
    if status == "open":
        query = query.where(DriverIssue.status.in_(driver_issues.OPEN_STATUSES))
    elif status in ISSUE_STATUSES:
        query = query.where(DriverIssue.status == status)
    elif status not in (None, "", "all"):
        fail(422, f"status must be open, all or one of {', '.join(ISSUE_STATUSES)}.", "validation")
    if priority in ISSUE_PRIORITIES:
        query = query.where(DriverIssue.priority == priority)
    if batch_id is not None:
        query = query.where(DriverIssue.batch_id == batch_id)
    if day is not None:
        start = datetime.combine(day, datetime.min.time(), MANILA)
        query = query.where(DriverIssue.created_at >= start, DriverIssue.created_at < start + timedelta(days=1))
    rows = list(db.execute(query.order_by(DriverIssue.created_at.desc(), DriverIssue.id.desc()).limit(limit)).scalars().all())
    weight = {"urgent": 0, "high": 1, "normal": 2}
    rows.sort(key=lambda r: (0 if r.status in driver_issues.OPEN_STATUSES else 1, weight.get(r.priority, 3)))
    return rows


def _issue_payloads(db: Session, rows: list[DriverIssue]) -> list[dict]:
    """Exactly the /issues item shape: driver_issues.public() + responded_by + photos (caption metadata)."""
    responders = {r.responded_by_user_id for r in rows if r.responded_by_user_id}
    names = users_by_id(db, responders)
    out = []
    for row in rows:
        item = driver_issues.public(row)
        item["responded_by"] = names.get(row.responded_by_user_id, {}).get("name") if row.responded_by_user_id else None
        item["photos"] = day_model.issue_photos(row)
        out.append(item)
    day_model.photo_capture_times(db, [p for item in out for p in item["photos"]])
    from services.order_tracker_facts import capture_issues
    capture_issues(db, out)
    return out


def issues_list(db: Session, status: str | None, priority: str | None, q: str | None = None, warehouse: str | None = None, day: date | None = None, limit: int = 200) -> list[dict]:
    """GET /issues with search (reference, SO, truck, reporter, category, description), warehouse (the trip driver's, else the
    reporter's staff record) and an optional Manila report date. Reads driver_issues (+ trips of those issues for the warehouse)."""
    rows = _issue_rows(db, status=status, priority=priority, day=day, limit=1000 if (q or warehouse) else limit)
    if warehouse and rows:
        drivers = {t.batch_id: t for t in db.execute(select(Trip).where(Trip.batch_id.in_({r.batch_id for r in rows if r.batch_id}))).scalars().all()}
        rows = [r for r in rows if ((_staff_warehouse(drivers[r.batch_id].driver_staff_id) or str(drivers[r.batch_id].warehouse or "").upper()) if r.batch_id in drivers else _staff_warehouse(r.reporter_staff_id)) == warehouse]
    needle = day_model.search_key(q)
    if needle:
        rows = [r for r in rows if day_model.matches(needle, [r.reference, r.so_number, r.vehicle, staff_name(r.reporter_staff_id) if r.reporter_staff_id is not None else None, r.category, r.description, r.batch_id])]
    return _issue_payloads(db, rows[:limit])


# ---- hard copies: who holds each signed carbon-copy invoice, and since when ---------------------------------------------------


def hardcopies(db: Session, warehouse: str | None, driver_id: int | None, q: str | None = None, day: date | None = None, collected: bool = False) -> dict:
    """Every invoice hard copy not yet validated as received, ALL dates (or only trips of `day`):
    with the driver (checked out of a stop, handover not submitted) / with the officer (handed over, not validated) /
    missing or discrepancy (validated with a problem, not resolved). `q` = the same search as the Trips list (SO, invoice,
    customer, driver / helper names, plate). Counts are over the filtered rows."""
    out: list[dict] = []
    needle = day_model.search_key(q)

    def trip_fields(trip) -> dict:
        return {"so_id": None, "vehicle": trip.vehicle, "driver_name": staff_name(trip.driver_staff_id), "helpers": [staff_name(h) for h in helper_ids_of(trip.driver_staff_id, trip.helper_staff_ids)]}

    def wanted(trip) -> bool:
        return day is None or trip.delivery_date == day
    trips = [] if collected else db.execute(select(Trip).where(Trip.status.in_(("on_trip", "returned")))).scalars().all()
    if trips:
        from models.trip import TripStop

        for stop in db.execute(select(TripStop).where(TripStop.trip_id.in_([t.id for t in trips]), TripStop.status == "checked_out")).scalars().all():
            trip = next(t for t in trips if t.id == stop.trip_id)
            if driver_id is not None and trip.driver_staff_id != driver_id:
                continue
            if (warehouse and (_staff_warehouse(trip.driver_staff_id) or str(trip.warehouse or "").upper()) != warehouse) or not wanted(trip):
                continue
            out.append({"delivery_outcome": stop.outcome, "so_number": stop.so_number, "customer_name": stop.customer_name, "invoice_no": trip_state.invoice_no_of(stop), "state": "with_driver", "state_label": "With the driver",
                        "holder": staff_name(trip.driver_staff_id), "since": iso(stop.checkout_at), "batch_id": trip.batch_id, "delivery_date": trip.delivery_date.isoformat(), "note": None,
                        **trip_fields(trip), "so_id": stop.so_id})
    from models.trip import TripStop
    stmt = (select(InvoiceHandoverItem, InvoiceHandover, Trip, TripStop.outcome)
            .join(InvoiceHandover, InvoiceHandover.id == InvoiceHandoverItem.handover_id)
            .join(Trip, Trip.id == InvoiceHandover.trip_id)
            .outerjoin(TripStop, (TripStop.trip_id == Trip.id) & (TripStop.so_id == InvoiceHandoverItem.so_id)))
    if collected:
        stmt = stmt.where(InvoiceHandoverItem.officer_status == "received")
        if day is not None:
            stmt = stmt.where(Trip.delivery_date == day)
        if driver_id is not None:
            stmt = stmt.where(Trip.driver_staff_id == driver_id)
        if warehouse:
            matching = [p["id"] for p in staff_directory_cache.all_staff() if _staff_warehouse(p["id"]) == warehouse]
            stmt = stmt.where(or_(Trip.driver_staff_id.in_(matching), func.upper(Trip.warehouse) == warehouse))
        if needle:
            named = [p["id"] for p in staff_directory_cache.all_staff() if day_model.matches(needle, [p.get("name")])]
            loose = re.sub(r"\d+", lambda m: str(int(m[0])), needle)
            pattern = "%" + "%".join(loose) + "%"
            stmt = stmt.where(or_(func.lower(InvoiceHandoverItem.so_number).like(pattern),
                                  func.lower(InvoiceHandoverItem.invoice_no).like(pattern),
                                  func.lower(InvoiceHandoverItem.customer_name).like(pattern),
                                  func.lower(Trip.vehicle).like(pattern), Trip.driver_staff_id.in_(named),
                                  *[cast(Trip.helper_staff_ids, String).contains(str(sid)) for sid in named]))
        stmt = stmt.order_by(InvoiceHandoverItem.officer_checked_at.desc(), InvoiceHandoverItem.id.desc()).limit(501)
    else:
        # Include received items of active legacy trips for authority/deduplication;
        # otherwise they can reappear as "With driver" from their checkout rows.
        stmt = stmt.where(or_(InvoiceHandoverItem.officer_status != "received", Trip.id.in_([t.id for t in trips])))
    found = db.execute(stmt).all()
    truncated = collected and len(found) > 500
    if collected:
        found = found[:500]
    authoritative = {(trip.batch_id, item.so_id) for item, _h, trip, _outcome in found}
    officer_ids = {uid for item, h, _t, _outcome in found for uid in (h.officer_user_id, item.officer_user_id) if uid is not None}
    names = users_by_id(db, officer_ids)
    for item, handover, trip, outcome in found:
        if not collected and item.officer_status == "received":
            continue
        if driver_id is not None and trip.driver_staff_id != driver_id:
            continue
        if (warehouse and (_staff_warehouse(trip.driver_staff_id) or str(trip.warehouse or "").upper()) != warehouse) or not wanted(trip):
            continue
        if item.officer_status == "received":
            state, label = "received", "Collected"
            holder = names.get(item.officer_user_id, {}).get("name") or "Logistics"
            since = item.resolved_at or item.officer_checked_at
        elif item.officer_status == "pending":
            officer = names.get(handover.officer_user_id, {}).get("name") if handover.officer_user_id is not None else None
            state, label, holder, since = ("with_officer", "With the officer: to validate", officer or "Logistics officer", handover.submitted_at) if item.handed_over else ("not_handed_over", "Not handed over by the driver", staff_name(trip.driver_staff_id), handover.submitted_at)
        else:
            state, label, holder, since = item.officer_status, "Missing" if item.officer_status == "missing" else "Discrepancy", staff_name(trip.driver_staff_id), item.officer_checked_at
        out.append({"delivery_outcome": outcome, "received_by": names.get(item.officer_user_id, {}).get("name"), "item_id": item.id, "handed_over": item.handed_over, "so_number": item.so_number, "customer_name": item.customer_name, "invoice_no": item.invoice_no, "state": state, "state_label": label, "holder": holder, "since": iso(since),
                    "batch_id": trip.batch_id, "delivery_date": trip.delivery_date.isoformat(), "note": item.officer_note or item.crew_note, **trip_fields(trip), "so_id": item.so_id})
    if needle:
        out = [r for r in out if day_model.matches(needle, [r["so_number"], r["invoice_no"], r["customer_name"], r["driver_name"], *r["helpers"], r["vehicle"]])]
    # A legacy returned trip can already have a handover. Its handover item is
    # authoritative; do not also show the same invoice as still with its driver.
    handed = authoritative
    out = [r for r in out if r["state"] != "with_driver" or (r["batch_id"], r["so_id"]) not in handed]
    out.sort(key=lambda r: r["since"] or "", reverse=collected)
    counts: dict[str, int] = defaultdict(int)
    for row in out:
        counts[row["state"]] += 1
    return {"generated_at": trip_events.now_utc().isoformat(), "total": len(out), "counts": dict(counts), "rows": out, "collected": collected, "truncated": truncated}
