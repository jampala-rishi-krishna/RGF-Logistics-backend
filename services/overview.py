"""Driver Overview read model for admin / dispatcher. Contract: docs/TRIP_WORKFLOW.md section 8 (KPI definitions are normative).

Reads ONLY Neon (sales_orders for batches, trips and their children) plus the in-memory staff directory / users. Never Zoho.
Results are cached 15 s per (endpoint, params, trip_events.generation(), live_sales_order_cache.assignment_generation()) so a 30 s
dashboard poll by several users costs at most one set of queries per generation per 15 s.
"""
from __future__ import annotations

import csv
import io
import os
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.trip import FormTemplate, FormTemplateItem, InvoiceHandover, InvoiceHandoverItem, Trip, TripEvent
from services import day_model, driver_issues, live_sales_order_cache, trip_state, staff_directory_cache, trip_events, warehouses
from services.staff_roles import staff_kind
from services.trip_events import fail
from services.trip_timeline import aware, iso, minutes_between, staff_name, users_by_id

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
        "sos": [{"so_id": o.so_id, "so_number": o.so_number, "customer_name": o.customer_name, "order_type": "TO" if str(o.so_id).startswith("TO:") else "SO"} for o in facts.orders],
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


EVENT_LABELS = {"acknowledged": "Acknowledged {so}", "trip_started": "Started the trip", "checked_in": "Arrived at {so}", "checked_out": "Checked out of {so}", "trip_ended": "Ended the trip",
                "handover_submitted": "Handed over invoices", "officer_acknowledged": "Officer received the invoices", "invoice_validated": "Invoice validated {so}", "invoice_resolved": "Invoice resolved {so}", "trip_closed": "Trip closed"}


def _batch_label(state: str, facts: day_model.BatchFacts) -> str:
    return _chip_label(state, facts.stops_done, facts.so_count)


def board(db: Session, day: date, warehouse: str | None, status: str | None, driver_id: int | None, stale_minutes: int) -> dict:
    """One row per PERSON. A person on several batches (two Assign clicks to the same truck) is ONE row whose progress is the SUM
    over all of their batches today, so it equals what their own app shows; `batches` lists each trip (Trip 1, Trip 2 ...)."""
    data = Data(db, day, day)
    batches = data.filtered(None, None)  # the warehouse filter applies to each PERSON's own staff record, below
    ids = [b["batch_id"] for b in batches]
    last_event: dict[str, TripEvent] = {}
    last_gps_event: dict[str, TripEvent] = {}
    if ids:
        for event in db.execute(select(TripEvent).where(TripEvent.batch_id.in_(ids)).order_by(TripEvent.client_ts, TripEvent.id)).scalars().all():
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
            action = {"type": latest.event_type, "at": iso(latest.client_ts), "label": EVENT_LABELS.get(latest.event_type, latest.event_type).format(so=so_numbers.get(latest.so_id, "") if latest.so_id else "").strip()}
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
                     "stale": state == "on_trip" and last_at is not None and (now - last_at) > timedelta(minutes=stale_minutes)})
    busy = set(per_person)
    for staff in _active_idle_drivers(warehouse, None):
        if staff["id"] not in busy:
            rows.append({"so_numbers": [], "staff_id": staff["id"], "name": staff.get("name") or f"Staff #{staff['id']}", "role": "driver", "warehouse": str(staff.get("warehouse") or "").upper() or None, "state": "idle", "state_label": "Idle",
                         "vehicle": None, "batch_id": None, "current_stop": None, "progress": {"done": 0, "total": 0}, "acknowledged": 0, "batch_count": 0, "kg": 0.0, "kg_complete": False, "kg_unknown_lines": 0, "packs": None, "trips": [],
                         "last_action": None, "last_gps_at": None, "stale": False})
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
                        "driver": {"id": batch["driver_id"], "name": staff_name(batch["driver_id"])}, "trip": summary_})
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
               "minutes_at_stop", "trip_started", "trip_ended", "handover_status", "invoice_status", "officer", "validated_at"]


def _local(value: datetime | None) -> str:
    value = aware(value)
    return value.astimezone(MANILA).isoformat(timespec="seconds") if value else ""


def _safe(value) -> str:
    """Stops spreadsheet formula injection: a text cell that would start a formula gets a leading apostrophe."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(db: Session, start: date, end: date, warehouse: str | None, driver_id: int | None) -> bytes:
    data = Data(db, start, end)
    batches = data.filtered(warehouse, driver_id)
    users = users_by_id(db, [a.by_user_id for b in batches for a in b["acks"].values()] + [b["handover"].officer_acknowledged_by_user_id for b in batches if b["handover"]] + [i.officer_user_id for b in batches for i in b["items"]])
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
                _local(trip.started_at) if trip else "", _local(trip.ended_at) if trip else "", handover.status if handover else "", item.officer_status if item else "", officer or "", validated]])
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


def trips_list(db: Session, start: date, end: date, warehouse: str | None, driver_id: int | None) -> dict:
    """One row per batch in the range with the numbers every screen shares (day_model) plus trip timings."""
    data = Data(db, start, end)
    rows = []
    numbering: dict[int, dict[str, tuple[int, int]]] = {}
    for staff_id, items in day_model.by_driver([b["facts"] for b in data.filtered(warehouse, driver_id)]).items():
        numbering[staff_id] = day_model.trip_numbers(items)
    for batch in sorted(data.filtered(warehouse, driver_id), key=lambda b: (b["date"], b["batch_id"]), reverse=True):
        f = batch["facts"]
        trip = batch["trip"]
        n, total = numbering.get(batch["driver_id"], {}).get(batch["batch_id"], (1, 1))
        rows.append({"batch_id": batch["batch_id"], "date": batch["date"].isoformat() if batch["date"] else None, "driver_id": batch["driver_id"], "driver_name": staff_name(batch["driver_id"]),
                     "helpers": [staff_name(h) for h in batch["helper_ids"]], "vehicle": batch["vehicle"], "trip_number": n, "trips_on_date": total, "state": f.state,
                     "state_label": _batch_label(f.state, f), "so_count": f.so_count, "kg": f.kg, "kg_complete": f.kg_complete, "kg_unknown_lines": f.kg_unknown_lines, "packs": f.packs,
                     "acknowledged": f.acknowledged, "stops_done": f.stops_done, "delivered": f.delivered, "partial": f.partial, "failed": f.failed,
                     "started_at": iso(trip.started_at) if trip else None, "ended_at": iso(trip.ended_at) if trip else None,
                     "trip_minutes": minutes_between(trip.started_at, trip.ended_at) if trip else None, "handover_status": batch["handover"].status if batch["handover"] else None})
    return {"from": start.isoformat(), "to": end.isoformat(), "total": len(rows), "trips": rows}


# ---- hard copies: who holds each signed carbon-copy invoice, and since when ---------------------------------------------------


def hardcopies(db: Session, warehouse: str | None, driver_id: int | None) -> dict:
    """Every invoice hard copy not yet validated as received, ALL dates:
    with the driver (checked out of a stop, handover not submitted) / with the officer (handed over, not validated) /
    missing or discrepancy (validated with a problem, not resolved)."""
    out: list[dict] = []
    trips = db.execute(select(Trip).where(Trip.status.in_(("on_trip", "returned")))).scalars().all()
    if trips:
        from models.trip import TripStop

        for stop in db.execute(select(TripStop).where(TripStop.trip_id.in_([t.id for t in trips]), TripStop.status == "checked_out")).scalars().all():
            trip = next(t for t in trips if t.id == stop.trip_id)
            if driver_id is not None and trip.driver_staff_id != driver_id:
                continue
            if warehouse and (_staff_warehouse(trip.driver_staff_id) or str(trip.warehouse or "").upper()) != warehouse:
                continue
            out.append({"so_number": stop.so_number, "customer_name": stop.customer_name, "invoice_no": trip_state.invoice_no_of(stop), "state": "with_driver", "state_label": "With the driver",
                        "holder": staff_name(trip.driver_staff_id), "since": iso(stop.checkout_at), "batch_id": trip.batch_id, "delivery_date": trip.delivery_date.isoformat(), "note": None})
    found = db.execute(select(InvoiceHandoverItem, InvoiceHandover, Trip).join(InvoiceHandover, InvoiceHandover.id == InvoiceHandoverItem.handover_id).join(Trip, Trip.id == InvoiceHandover.trip_id)
                       .where(InvoiceHandoverItem.officer_status != "received")).all()
    officer_ids = {h.officer_user_id for _i, h, _t in found if h.officer_user_id is not None}
    names = users_by_id(db, officer_ids)
    for item, handover, trip in found:
        if driver_id is not None and trip.driver_staff_id != driver_id:
            continue
        if warehouse and (_staff_warehouse(trip.driver_staff_id) or str(trip.warehouse or "").upper()) != warehouse:
            continue
        if item.officer_status == "pending":
            officer = names.get(handover.officer_user_id, {}).get("name") if handover.officer_user_id is not None else None
            state, label, holder, since = ("with_officer", "With the officer: to validate", officer or "Logistics officer", handover.submitted_at) if item.handed_over else ("not_handed_over", "Not handed over by the driver", staff_name(trip.driver_staff_id), handover.submitted_at)
        else:
            state, label, holder, since = item.officer_status, "Missing" if item.officer_status == "missing" else "Discrepancy", staff_name(trip.driver_staff_id), item.officer_checked_at
        out.append({"so_number": item.so_number, "customer_name": item.customer_name, "invoice_no": item.invoice_no, "state": state, "state_label": label, "holder": holder, "since": iso(since),
                    "batch_id": trip.batch_id, "delivery_date": trip.delivery_date.isoformat(), "note": item.officer_note or item.crew_note})
    out.sort(key=lambda r: r["since"] or "")
    counts: dict[str, int] = defaultdict(int)
    for row in out:
        counts[row["state"]] += 1
    return {"generated_at": trip_events.now_utc().isoformat(), "total": len(out), "counts": dict(counts), "rows": out}
