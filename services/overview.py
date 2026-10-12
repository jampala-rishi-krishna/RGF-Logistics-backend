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

from models.sales_order_history import SalesOrderHistory
from models.trip import FormTemplate, FormTemplateItem, InvoiceHandover, InvoiceHandoverItem, SoAcknowledgement, Trip, TripEvent, TripStop
from services import live_sales_order_cache, staff_directory_cache, trip_events, warehouses
from services.staff_roles import helper_ids_of, staff_kind
from services.trip_events import fail
from services.trip_timeline import VISIBLE_STATUSES, aware, chip_for, iso, minutes_between, staff_name, users_by_id

MANILA = ZoneInfo("Asia/Manila")
CACHE_SECONDS = 15
MAX_RANGE_DAYS = 366
WAREHOUSE_CODES = ("METS", "GLACIER")
STATE_PRIORITY = ("on_trip", "ready", "to_acknowledge", "returned", "handover_pending", "closed")
CHIP_LABELS = {"to_acknowledge": "To acknowledge", "ready": "Ready to start", "returned": "Back - hand over invoices", "handover_pending": "Handover pending", "closed": "Closed"}
OUTCOME_BUCKET = {"delivered": "delivered", "partially delivered": "partial", "failed": "failed", "returned": "failed", "failed / returned": "failed"}

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
    try:
        return max(1, int(os.environ.get("DRIVER_STALE_MINUTES", "15")))
    except ValueError:
        return 15


# ---- loading --------------------------------------------------------------------------------------------------------


def _staff_warehouse(staff_id: int | None) -> str | None:
    staff = staff_directory_cache.get_by_id(staff_id) if staff_id is not None else None
    value = str((staff or {}).get("warehouse") or "").strip().upper()
    return value or None


def _batch_warehouse(batch: dict) -> str | None:
    return _staff_warehouse(batch["driver_id"]) or (str(batch["trip"].warehouse).upper() if batch["trip"] is not None and batch["trip"].warehouse else None)


class Data:
    """Everything the overview needs for a date range, loaded with a fixed number of queries."""

    def __init__(self, db: Session, start: date, end: date):
        rows = db.execute(select(SalesOrderHistory).where(SalesOrderHistory.expected_shipment_date >= start, SalesOrderHistory.expected_shipment_date <= end,
                                                          SalesOrderHistory.assignment_status.in_(VISIBLE_STATUSES), SalesOrderHistory.assignment_batch_id.is_not(None))).scalars().all()
        trips = db.execute(select(Trip).where(Trip.delivery_date >= start, Trip.delivery_date <= end)).scalars().all()
        trip_ids = [t.id for t in trips]
        self.acks = defaultdict(dict)
        self.stops = defaultdict(list)
        self.handovers: dict[int, InvoiceHandover] = {}
        self.items = defaultdict(list)
        if trip_ids:
            for a in db.execute(select(SoAcknowledgement).where(SoAcknowledgement.trip_id.in_(trip_ids))).scalars().all():
                self.acks[a.trip_id][a.so_id] = a
            for s in db.execute(select(TripStop).where(TripStop.trip_id.in_(trip_ids)).order_by(TripStop.sequence)).scalars().all():
                self.stops[s.trip_id].append(s)
            self.handovers = {h.trip_id: h for h in db.execute(select(InvoiceHandover).where(InvoiceHandover.trip_id.in_(trip_ids))).scalars().all()}
            if self.handovers:
                for i in db.execute(select(InvoiceHandoverItem).where(InvoiceHandoverItem.handover_id.in_([h.id for h in self.handovers.values()])).order_by(InvoiceHandoverItem.id)).scalars().all():
                    self.items[i.handover_id].append(i)
        grouped: dict[str, list[SalesOrderHistory]] = defaultdict(list)
        for row in rows:
            grouped[row.assignment_batch_id].append(row)
        trip_by_batch = {t.batch_id: t for t in trips}
        self.batches: dict[str, dict] = {}
        for batch_id in set(grouped) | set(trip_by_batch):
            batch_rows = sorted(grouped.get(batch_id, []), key=lambda r: (aware(r.assigned_at).timestamp() if r.assigned_at else 0.0, str(r.salesorder_number or r.id)))
            trip = trip_by_batch.get(batch_id)
            first = batch_rows[0] if batch_rows else None
            driver_id = trip.driver_staff_id if trip is not None else first.driver_id
            helpers = helper_ids_of(driver_id, trip.helper_staff_ids if trip is not None else first.helper_ids)
            stops = self.stops.get(trip.id, []) if trip is not None else []
            sos = [{"so_id": r.id, "so_number": r.salesorder_number or r.id, "customer_name": r.customer_name} for r in batch_rows]
            known = {s["so_id"] for s in sos}
            sos += [{"so_id": s.so_id, "so_number": s.so_number or s.so_id, "customer_name": s.customer_name} for s in stops if s.so_id not in known]
            acks = self.acks.get(trip.id, {}) if trip is not None else {}
            handover = self.handovers.get(trip.id) if trip is not None else None
            self.batches[batch_id] = {
                "batch_id": batch_id, "date": trip.delivery_date if trip is not None else first.expected_shipment_date, "driver_id": driver_id, "helper_ids": helpers,
                "vehicle": trip.vehicle if trip is not None else first.vehicle_id, "sos": sos, "trip": trip, "acks": acks, "stops": stops, "handover": handover, "items": self.items.get(handover.id, []) if handover else [],
            }

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
    trip = batch["trip"]
    return chip_for(trip.status if trip is not None else None, len(batch["acks"]), len(batch["sos"]))


def _chip_label(state: str, done: int, total: int) -> str:
    if state == "on_trip":
        return f"On trip · stop {min(done + 1, max(total, 1))} of {total}"
    return CHIP_LABELS.get(state, state)


def _outcome_bucket(outcome: str | None) -> str | None:
    return OUTCOME_BUCKET.get(str(outcome or "").strip().lower())


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
    by_driver: dict[int, list[dict]] = defaultdict(list)
    for batch in batches:
        if batch["driver_id"] is not None:
            by_driver[batch["driver_id"]].append(batch)
    done_states = ("returned", "handover_pending", "closed")
    on_trip = sum(1 for bs in by_driver.values() if any(batch_state(b) == "on_trip" for b in bs))
    returned = sum(1 for bs in by_driver.values() if all(batch_state(b) in done_states for b in bs))
    idle = [s for s in _active_idle_drivers(warehouse, driver_id) if s["id"] not in by_driver]
    sos = {"assigned": 0, "acknowledged": 0, "delivered": 0, "partial": 0, "failed": 0}
    for batch in batches:
        sos["assigned"] += len(batch["sos"])
        sos["acknowledged"] += len(batch["acks"])
        for stop in batch["stops"]:
            if stop.status == "checked_out":
                bucket = _outcome_bucket(stop.outcome)
                if bucket:
                    sos[bucket] += 1
    pending_handover = sum(1 for b in batches if b["trip"] is not None and b["trip"].status in ("returned", "handover_submitted"))
    return {"from": start.isoformat(), "to": end.isoformat(), "generated_at": trip_events.now_utc().isoformat(),
            "kpis": {"drivers": {"active": len(by_driver), "on_trip": on_trip, "returned": returned, "idle": len(idle)}, "sos": sos, "trips_pending_handover": pending_handover,
                     "invoices_outstanding": outstanding_invoices(db, warehouse, driver_id)}}


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


def board(db: Session, day: date, warehouse: str | None, status: str | None, driver_id: int | None, stale_minutes: int) -> dict:
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

    def person_rows(batch: dict):
        for staff_id, role in [(batch["driver_id"], "driver")] + [(h, "helper") for h in batch["helper_ids"]]:
            if staff_id is not None:
                yield staff_id, role

    best: dict[int, tuple[int, dict]] = {}
    for batch in batches:
        state = batch_state(batch)
        done = sum(1 for s in batch["stops"] if s.status == "checked_out")
        numbers = {s["so_id"]: s["so_number"] for s in batch["sos"]}
        current = next((s for s in batch["stops"] if s.status == "checked_in"), None)
        event = last_event.get(batch["batch_id"])
        gps_event = last_gps_event.get(batch["batch_id"])
        last_at = aware(event.client_ts) if event else (aware(batch["trip"].started_at) if batch["trip"] is not None else None)
        action = None
        if event is not None:
            action = {"type": event.event_type, "at": iso(event.client_ts), "label": EVENT_LABELS.get(event.event_type, event.event_type).format(so=numbers.get(event.so_id, "") if event.so_id else "").strip()}
        for staff_id, role in person_rows(batch):
            staff = staff_directory_cache.get_by_id(staff_id) or {}
            row = {"staff_id": staff_id, "name": staff.get("name") or f"Staff #{staff_id}", "role": role, "warehouse": str(staff.get("warehouse") or "").upper() or None, "state": state,
                   "vehicle": batch["vehicle"], "batch_id": batch["batch_id"], "current_stop": current.customer_name if current else None, "progress": {"done": done, "total": len(batch["sos"])},
                   "last_action": action, "last_gps_at": iso(gps_event.client_ts) if gps_event else None,
                   "stale": state == "on_trip" and last_at is not None and (now - last_at) > timedelta(minutes=stale_minutes)}
            rank = STATE_PRIORITY.index(state)
            if staff_id not in best or rank < best[staff_id][0]:
                best[staff_id] = (rank, row)
    rows = [row for _, row in best.values()]
    busy_drivers = {r["staff_id"] for r in rows if r["role"] == "driver"}
    for staff in _active_idle_drivers(warehouse, None):
        if staff["id"] not in busy_drivers and staff["id"] not in best:
            rows.append({"staff_id": staff["id"], "name": staff.get("name") or f"Staff #{staff['id']}", "role": "driver", "warehouse": str(staff.get("warehouse") or "").upper() or None, "state": "idle",
                         "vehicle": None, "batch_id": None, "current_stop": None, "progress": {"done": 0, "total": 0}, "last_action": None, "last_gps_at": None, "stale": False})
    if warehouse:
        rows = [r for r in rows if r["warehouse"] == warehouse]
    if driver_id is not None:
        rows = [r for r in rows if r["staff_id"] == driver_id]
    if status:
        rows = [r for r in rows if r["state"] == status]
    order = {state: i for i, state in enumerate(STATE_PRIORITY + ("idle",))}
    rows.sort(key=lambda r: (order.get(r["state"], 99), str(r["name"]).lower()))
    return {"stale_minutes": stale_minutes, "rows": rows}


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
            add("checkin", stop.checkin_gps, stop.checkin_at, batch, stop.so_number, stop.customer_name)
            add("checkout", stop.checkout_gps, stop.checkout_at, batch, stop.so_number, stop.customer_name, stop.outcome)
        if trip is not None:
            add("end", trip.ended_gps, trip.ended_at, batch)
    points.sort(key=lambda p: (p["at"] or "", p["batch_id"]))
    return {"warehouses": [{"id": w["id"], "name": w["name"], "lat": w["lat"], "lng": w["lng"]} for w in warehouses.list_warehouses()], "points": points}


# ---- CSV export -----------------------------------------------------------------------------------------------------

CSV_COLUMNS = ["date", "batch", "driver", "helpers", "vehicle", "so_number", "customer", "acknowledged_at", "acknowledged_by", "checkin_at", "distance_m", "checkout_at", "outcome", "reason",
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
                (stop.outcome if stop else "") or "", (stop.reason if stop else "") or "", "" if not stop or minutes_between(stop.checkin_at, stop.checkout_at) is None else minutes_between(stop.checkin_at, stop.checkout_at),
                _local(trip.started_at) if trip else "", _local(trip.ended_at) if trip else "", handover.status if handover else "", item.officer_status if item else "", officer or "", validated]])
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


# ---- templates ------------------------------------------------------------------------------------------------------

KINDS = ("acknowledge_checklist", "delivery_checkout")
ITEM_TYPES = {"acknowledge_checklist": ("checkbox", "count", "photo", "note"),
              "delivery_checkout": ("text", "textarea", "dropdown", "radio", "number", "date", "datetime", "photo", "signature", "checkbox")}
PREFILLS = ("so_number", "customer", "address", "driver", "vehicle", "helper", "date")


def _template_payload(template: FormTemplate, items: list[FormTemplateItem], *, only_active_items: bool) -> dict:
    shown = [i for i in items if i.active] if only_active_items else items
    return {"id": template.id, "kind": template.kind, "version": template.version, "name": template.name, "is_draft": template.is_draft, "active": template.active, "source_note": template.source_note,
            "items": [{"id": i.id, "key": i.key, "label": i.label, "item_type": i.item_type, "required": i.required, "options": i.options, "prefill": i.prefill, "show_when": i.show_when, "position": i.position, "active": i.active}
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
    if kind == "delivery_checkout" and not any(i.get("key") == "outcome" and i.get("item_type") in ("dropdown", "radio") for i in items):
        errors.append({"key": "outcome", "message": 'The checkout form needs an "outcome" dropdown or radio item (the trip logic reads it).'})
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
                                options=item.get("options") or None, prefill=prefill, show_when=item.get("show_when") or None, active=item.get("active", True) is not False))
    db.commit()
    trip_events.bump()
    return get_template_version(db, kind, created.version)
