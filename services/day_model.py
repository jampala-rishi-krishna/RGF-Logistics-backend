"""THE read model of a day's assignments. Everything that shows "how many SOs / kg / packs / batches / acknowledged / delivered"
(driver app Today + History, Driver Overview KPIs + board, the reconciliation check) goes through `facts_for_rows` and `totals`
below, so the screens cannot disagree: there is no second implementation to drift.

Identity of a batch = `assignment_batch_id` (one Assign click = one truck). Two batches of the same vehicle are never merged.
Rows saved before that column existed get a stable `legacy-...` key (same truck + same assigned_at).

Reads Neon (sales_orders, sales_order_lines, trips and their children) only; never Zoho. Read-only: nothing here writes.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session

from models.sales_order_history import SalesOrderHistory
from models.sales_order_lines import SalesOrderLine
from models.trip import Trip
from services.pack_units import pack_total
from services.staff_roles import helper_ids_of

# Everything except never-assigned / unassigned / released orders is part of a driver's day or history.
VISIBLE_STATUSES = ("assigned", "manifested", "completed", "delivered")
OUTCOME_BUCKET = {"delivered": "delivered", "partially delivered": "partial", "failed": "failed", "returned": "failed", "failed / returned": "failed",
                  # cancelled at the customer / sales agent's request: not delivered for every count (services.cancel_request)
                  "cancelled_by_request": "failed", "cancelled by request": "failed"}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def legacy_batch_key(row) -> str:
    """Stable id for an assignment saved before assignment_batch_id existed: same truck + same assigned_at."""
    stamp = int(_aware(row.assigned_at).timestamp()) if row.assigned_at else 0
    return f"legacy-{stamp}-{hashlib.sha1(str(row.vehicle_id or '').encode('utf-8')).hexdigest()[:8]}"


def batch_key(row) -> str:
    return row.assignment_batch_id or legacy_batch_key(row)


def sort_key(row):
    assigned = _aware(row.assigned_at)
    return (assigned.timestamp() if assigned else 0.0, str(row.salesorder_number or row.id))


def outcome_bucket(outcome: str | None) -> str | None:
    return OUTCOME_BUCKET.get(str(outcome or "").strip().lower())


def is_crew(row, staff_id: int) -> bool:
    """Exact membership: the row's driver or one of its helpers (never a substring match: id 1 is not id 17)."""
    return staff_id == row.driver_id or staff_id in (row.helper_ids or [])


def crew_clause(staff_id: int):
    # Cheap SQL prefilter (substring of the JSON text); the exact test is is_crew() in Python.
    return or_(SalesOrderHistory.driver_id == staff_id, cast(SalesOrderHistory.helper_ids, String).contains(str(staff_id)))


# ---- per-order totals -------------------------------------------------------------------------------------------------------


def _num(value) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def order_totals(lines: list) -> dict:
    """kg = sum of the KNOWN line weights; `kg_complete` is False while any line has no weight (so a total is never presented as
    final when part of it is unknown). packs = None when no line is counted in packs (unknown, not zero)."""
    known = [line for line in lines if line.weight_kg is not None]
    return {
        "kg": round(sum(_num(line.weight_kg) for line in known), 2),
        "kg_complete": bool(lines) and len(known) == len(lines),
        "kg_unknown_lines": len(lines) - len(known),
        "packs": pack_total(lines),
    }


# ---- facts of one batch -------------------------------------------------------------------------------------------------------


@dataclass
class OrderFacts:
    so_id: str
    so_number: str
    customer_name: str | None
    kg: float
    kg_complete: bool
    packs: float | None
    kg_unknown_lines: int = 0


@dataclass
class BatchFacts:
    batch_id: str
    date: date | None
    driver_id: int | None
    helper_ids: list[int]
    vehicle: str | None
    orders: list[OrderFacts]
    trip: object | None  # models.trip.Trip
    acks: dict  # so_id -> SoAcknowledgement (this batch's SOs only)
    stops: dict  # so_id -> TripStop (this batch's SOs only)
    handover: object | None
    items: list = field(default_factory=list)  # InvoiceHandoverItem
    assigned_at: datetime | None = None  # first assignment click of the batch: orders "Trip 1", "Trip 2" of a day
    # What the assignment rows say NOW (the trip row freezes vehicle / crew when the crew first acts): used to detect "assignment changed".
    current_vehicle: str | None = None
    current_crew_ids: tuple = ()
    current_so_ids: tuple = ()
    current_updated_at: datetime | None = None
    acknowledged: int = 0
    stops_done: int = 0
    delivered: int = 0
    partial: int = 0
    failed: int = 0
    state: str = "to_acknowledge"

    @property
    def so_count(self) -> int:
        return len(self.orders)

    @property
    def so_ids(self) -> list[str]:
        return [order.so_id for order in self.orders]

    @property
    def kg(self) -> float:
        return round(sum(order.kg for order in self.orders), 2)

    @property
    def kg_complete(self) -> bool:
        return bool(self.orders) and all(order.kg_complete for order in self.orders)

    @property
    def kg_unknown_lines(self) -> int:
        return sum(order.kg_unknown_lines for order in self.orders)

    @property
    def packs(self) -> float | None:
        known = [order.packs for order in self.orders if order.packs is not None]
        return round(sum(known), 2) if known else None

    @property
    def crew_ids(self) -> list[int]:
        return ([self.driver_id] if self.driver_id is not None else []) + list(self.helper_ids)


def chip_for(status: str | None, acknowledged: int, total: int) -> str:
    """Display chip of a batch (docs/TRIP_WORKFLOW.md section 2)."""
    if status in (None, "pending"):
        return "to_acknowledge" if acknowledged < total or total == 0 else "ready"
    return {"on_trip": "on_trip", "returned": "returned", "handover_submitted": "handover_pending", "closed": "closed"}.get(status, "to_acknowledge")


def progress(so_ids: list[str], trip, acks: dict, stops: dict) -> dict:
    """Acknowledged / checked-out / outcome counts of a batch, restricted to its own SOs. ONE implementation."""
    acknowledged = sum(1 for so_id in so_ids if so_id in acks)
    done = [stops[so_id] for so_id in so_ids if so_id in stops and stops[so_id].status == "checked_out"]
    out = {"acknowledged": acknowledged, "stops_done": len(done), "delivered": 0, "partial": 0, "failed": 0}
    for stop in done:
        bucket = outcome_bucket(stop.outcome)
        if bucket:
            out[bucket] += 1
    return out


def facts_for_rows(batch_id: str, rows: list, lines_by_order: dict[str, list], data, *, items_by_handover: dict | None = None, trip_only: Trip | None = None) -> BatchFacts:
    """Facts of ONE batch from its sales-order rows (+ the trip data loaded by services.trip_state.load).
    `trip_only` builds a batch from its trip alone (its SO rows were released/reassigned after the trip started)."""
    trip = data.trips.get(batch_id)
    all_acks = data.acks.get(trip.id, {}) if trip else {}
    all_stops = data.stops.get(trip.id, {}) if trip else {}
    if rows:
        rows = sorted(rows, key=sort_key)
        first = rows[0]
        orders = []
        for row in rows:
            totals_ = order_totals(lines_by_order.get(row.id, []))
            orders.append(OrderFacts(row.id, row.salesorder_number or row.id, row.customer_name, totals_["kg"], totals_["kg_complete"], totals_["packs"], totals_["kg_unknown_lines"]))
        driver_id = trip.driver_staff_id if trip is not None else first.driver_id
        helpers = helper_ids_of(driver_id, trip.helper_staff_ids if trip is not None else first.helper_ids)
        vehicle = trip.vehicle if trip is not None else first.vehicle_id
        day = trip.delivery_date if trip is not None else first.expected_shipment_date
    else:  # trip only: the SO list is the frozen stop list
        orders = [OrderFacts(stop.so_id, stop.so_number or stop.so_id, stop.customer_name, 0.0, False, None) for stop in sorted(all_stops.values(), key=lambda s: s.sequence)]
        driver_id = trip_only.driver_staff_id
        helpers = helper_ids_of(driver_id, trip_only.helper_staff_ids)
        vehicle, day = trip_only.vehicle, trip_only.delivery_date
    so_ids = [order.so_id for order in orders]
    acks = {so_id: all_acks[so_id] for so_id in so_ids if so_id in all_acks}
    stops = {so_id: all_stops[so_id] for so_id in so_ids if so_id in all_stops}
    handover = data.handovers.get(batch_id)
    prog = progress(so_ids, trip, acks, stops)
    facts = BatchFacts(batch_id=batch_id, date=day, driver_id=driver_id, helper_ids=list(helpers), vehicle=vehicle, orders=orders, trip=trip, acks=acks, stops=stops, handover=handover,
                       items=(items_by_handover or {}).get(handover.id, []) if handover is not None else [], **prog)
    facts.assigned_at = _aware(rows[0].assigned_at) if rows else None
    if rows:
        facts.current_vehicle = rows[0].vehicle_id
        facts.current_crew_ids = tuple(sorted({rows[0].driver_id, *helper_ids_of(rows[0].driver_id, rows[0].helper_ids)} - {None}))
        facts.current_so_ids = tuple(sorted(row.id for row in rows))
        stamps = [_aware(row.updated_at) for row in rows if getattr(row, "updated_at", None) is not None]
        facts.current_updated_at = max(stamps) if stamps else facts.assigned_at
    facts.state = chip_for(trip.status if trip is not None else None, facts.acknowledged, facts.so_count)
    return facts


# ---- ONE vocabulary: driver app rail (1 Load / 2 Deliver / 3 Return) = Driver Overview tiles + attention flags (all pure) ------

RETURN_STATUSES = ("returned", "handover_submitted")  # "returned" = legacy trips ended before the automatic handover
CREW_ROLES = ("driver", "helper")
UNASSIGNED = "order_unassigned"
GPS_EVENTS = ("acknowledged", "trip_started", "checked_in", "checked_out", "trip_ended")  # crew actions that capture GPS
FAR_CHECKIN_M = 500.0
TILE_OF_STEP = {1: "loading", 2: "on_road", 3: "back", "closed": "closed"}
ATTENTION_LABELS = {  # order = display order of the chips
    "no_activity": "No activity", "not_delivered": "Not delivered", "partial": "Partial", "problem_open": "Problem reported", "far_checkin": "Far check-in",
    "no_gps": "No GPS", "invoice_missing": "Invoice missing", "invoice_discrepancy": "Invoice wrong", "order_removed": "Order removed", "photo_missing": "Photo missing",
}


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def _has_gps(gps) -> bool:
    return isinstance(gps, dict) and gps.get("lat") is not None and gps.get("lng") is not None


def _status(facts: BatchFacts) -> str:
    return facts.trip.status if facts.trip is not None else "pending"


def _ordered_stop_ids(facts: BatchFacts) -> list[str]:
    """The batch's own SOs in delivery order (frozen stop sequence once started, else assignment order)."""
    sequence = {so_id: getattr(stop, "sequence", None) for so_id, stop in facts.stops.items()}
    ids = facts.so_ids
    return sorted(ids, key=lambda so_id: (sequence.get(so_id) if sequence.get(so_id) is not None else 10 ** 6, ids.index(so_id)))


def invoice_counts(facts: BatchFacts) -> dict:
    """Signed invoice copies of a batch. Before End trip there is no handover: every checked-out stop's copy is with the driver.
    After it, the officer's status per item (pending + handed over = with the officer; pending + not handed over = with the driver)."""
    if facts.handover is None:
        return {"total": facts.so_count, "received": 0, "missing": 0, "discrepancy": 0, "with_driver": facts.stops_done, "with_officer": 0}
    status = [(str(i.officer_status or "pending").lower(), bool(i.handed_over)) for i in facts.items]
    return {"total": len(status), "received": sum(1 for s, _ in status if s == "received"), "missing": sum(1 for s, _ in status if s == "missing"),
            "discrepancy": sum(1 for s, _ in status if s == "discrepancy"), "with_driver": sum(1 for s, handed in status if s == "pending" and not handed),
            "with_officer": sum(1 for s, handed in status if s == "pending" and handed)}


def driver_step(facts: BatchFacts) -> dict:
    """Where a trip is on the 3-step rail - the SAME answer for the driver app rail and the Driver Overview tiles (CONTRACT).
    step 1 Load (trip pending / no trip row): "waiting_crew" | "ready";  step 2 Deliver (on_trip): "on_road";
    step 3 Return (returned [legacy] / handover_submitted): "invoices_due";  "closed": "closed". Every sub carries `problems`."""
    status = _status(facts)
    problems = facts.partial + facts.failed
    if status == "closed":
        inv = invoice_counts(facts)
        return {"step": "closed", "label_key": "closed", "sub": {"received": inv["received"], "total_invoices": inv["total"], "problems": problems}}
    if status in RETURN_STATUSES:
        inv = invoice_counts(facts)
        return {"step": 3, "label_key": "invoices_due", "sub": {"received": inv["received"], "total_invoices": inv["total"], "missing": inv["missing"], "discrepancy": inv["discrepancy"], "problems": problems}}
    if status == "on_trip":
        ordered = _ordered_stop_ids(facts)
        here = next((so_id for so_id in ordered if so_id in facts.stops and facts.stops[so_id].status == "checked_in"), None)
        nxt = here or next((so_id for so_id in ordered if not (so_id in facts.stops and facts.stops[so_id].status == "checked_out")), None)
        numbers = {order.so_id: order.so_number for order in facts.orders}
        return {"step": 2, "label_key": "on_road", "sub": {"done": facts.stops_done, "total": facts.so_count, "current": ordered.index(nxt) + 1 if nxt else None,
                                                         "current_so": numbers.get(nxt) if nxt else None, "here_since": _iso(facts.stops[here].checkin_at) if here else None, "problems": problems}}
    ready = facts.so_count > 0 and facts.acknowledged >= facts.so_count
    return {"step": 1, "label_key": "ready" if ready else "waiting_crew", "sub": {"acknowledged": facts.acknowledged, "total": facts.so_count, "problems": problems}}


def tile_of(step: dict) -> str:
    """Driver Overview tile of a trip: loading | on_road | back | closed ("attention" is independent: any attention flag)."""
    return TILE_OF_STEP.get(step.get("step"), "loading")


# ---- photos (metadata only: the bytes live in object storage) -----------------------------------------------------------------


def _photo_id(value) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def stop_photos(stop, so_number: str | None) -> list[dict]:
    """Proof photos / signatures stored with a stop's check-out answers: {photo_id, so_number, captured_at (the stored client time of
    the check-out), kind (delivered | partial | failed from the stop outcome; signature = other), field_key}."""
    if stop is None:
        return []
    bucket = outcome_bucket(stop.outcome)
    out = []
    for answer in stop.form_answers or []:
        if not isinstance(answer, dict):
            continue
        key, kind_of_item = str(answer.get("key") or ""), answer.get("type")
        signature = kind_of_item == "signature" or key.endswith("signature")
        if not (kind_of_item in ("photo", "signature") or key.endswith("photo") or signature):
            continue
        photo_id = _photo_id(answer.get("value"))
        if photo_id is not None:
            out.append({"photo_id": photo_id, "so_number": so_number, "captured_at": _iso(stop.checkout_at), "kind": "other" if signature else (bucket or "other"), "field_key": key})
    return out


def ack_photos(ack, so_number: str | None) -> list[dict]:
    """Photos answered on the acknowledge checklist (kind other, captured at the acknowledgement's client time)."""
    out = []
    for answer in (getattr(ack, "answers", None) or []) if ack is not None else []:
        photo_id = _photo_id(answer.get("photo_id")) if isinstance(answer, dict) else None
        if photo_id is not None:
            out.append({"photo_id": photo_id, "so_number": so_number, "captured_at": _iso(ack.acknowledged_at), "kind": "other", "field_key": str(answer.get("key") or "photo")})
    return out


def issue_photos(issue) -> list[dict]:
    """Photos attached to a reported problem (kind problem, captured at the report's client time)."""
    at = _iso(getattr(issue, "client_ts", None) or getattr(issue, "created_at", None))
    return [{"photo_id": int(pid), "so_number": issue.so_number, "captured_at": at, "kind": "problem", "field_key": "issue_photo"} for pid in (issue.photo_ids or []) if _photo_id(pid) is not None]


# ---- attention ------------------------------------------------------------------------------------------------------------


def photo_capture_times(db: Session, photos: list[dict]) -> None:
    """Caption timestamps from trip_photos, never Drive: new created_at = client
    capture time, legacy created_at = upload time. Missing rows keep action time.
    """
    from models.trip import TripPhoto

    ids = {p["photo_id"] for p in photos}
    if not ids:
        return
    stamps = {p.id: _iso(p.created_at) for p in db.execute(select(TripPhoto).where(TripPhoto.id.in_(ids))).scalars().all()}
    for photo in photos:
        if stamps.get(photo["photo_id"]):
            photo["captured_at"] = stamps[photo["photo_id"]]


def last_crew_activity(events) -> datetime | None:
    """Newest crew action (driver / helper trip_events). A dispatcher's order_unassigned or an officer action is not crew activity."""
    stamps = [_aware(e.client_ts) for e in events if e.event_type != UNASSIGNED and e.role in CREW_ROLES and e.client_ts is not None]
    return max(stamps) if stamps else None


def _photo_missing(stop, rules: list[dict]) -> bool:
    answers = {str(a.get("key")): a.get("value") for a in (stop.form_answers or []) if isinstance(a, dict)}
    answers.setdefault("outcome", stop.outcome)
    for rule in rules:
        if rule.get("item_type") != "photo" or not rule.get("required"):
            continue
        show = rule.get("show_when")
        if isinstance(show, dict) and show.get("key") and answers.get(show["key"]) not in (show.get("in") or []):
            continue  # not asked for this outcome
        if answers.get(rule["key"]) in (None, ""):
            return True
    return False


def attention_hits(facts: BatchFacts, now: datetime, stale_minutes: int, *, events=(), open_issues=(), photo_rules: dict | None = None) -> list[dict]:
    """Every attention finding of a batch, each pointing at its stop (`stop_so_id`) or event (`event_id`) when there is one. Pure:
    `events` = the batch's trip_events rows, `open_issues` = its OPEN driver_issues rows, `photo_rules` = {form_template_id: [item dicts
    key/item_type/required/show_when]} of the check-out templates used (None = photo_missing is not evaluated)."""
    hits: list[dict] = []

    def hit(code, so_id=None, event_id=None, **extra):
        entry = {"code": code, "label": ATTENTION_LABELS[code]}
        if so_id is not None:
            entry["stop_so_id"] = so_id
        if event_id is not None:
            entry["event_id"] = event_id
        hits.append({**entry, **extra})

    events = sorted(events, key=lambda e: (_aware(e.client_ts) or datetime.min.replace(tzinfo=timezone.utc), e.id or 0))
    status = _status(facts)
    if status == "on_trip":
        last = last_crew_activity(events) or _aware(facts.trip.started_at)
        if last is not None and (_aware(now) - last).total_seconds() > stale_minutes * 60:
            hit("no_activity", minutes=int((_aware(now) - last).total_seconds() // 60))
    ordered = [facts.stops[so_id] for so_id in _ordered_stop_ids(facts) if so_id in facts.stops]
    done = [s for s in ordered if s.status == "checked_out"]
    for code, bucket in (("not_delivered", "failed"), ("partial", "partial")):
        for stop in done:
            if outcome_bucket(stop.outcome) == bucket:
                hit(code, stop.so_id)
    for issue in open_issues:
        hit("problem_open", issue.so_id, issue_id=issue.id)
    for stop in ordered:
        if stop.checkin_at is not None and (stop.checkin_far or (stop.checkin_distance_m is not None and stop.checkin_distance_m > FAR_CHECKIN_M)):
            hit("far_checkin", stop.so_id)
    no_gps_stops = set()
    for event in events:
        if event.event_type in GPS_EVENTS and event.role in CREW_ROLES and not _has_gps(event.gps):
            hit("no_gps", event.so_id, event.id)
            no_gps_stops.add(event.so_id)
    for stop in ordered:  # rows written without an event (or before events existed)
        if stop.so_id not in no_gps_stops and ((stop.checkin_at is not None and not _has_gps(stop.checkin_gps)) or (stop.checkout_at is not None and not _has_gps(stop.checkout_gps))):
            hit("no_gps", stop.so_id)
    for code, officer_status in (("invoice_missing", "missing"), ("invoice_discrepancy", "discrepancy")):
        for item in facts.items:
            if str(item.officer_status or "").lower() == officer_status:
                hit(code, item.so_id)
    for event in events:
        if event.event_type == UNASSIGNED and (event.result or {}).get("previous_state") not in (None, "assigned"):
            hit("order_removed", event.so_id, event.id)  # removed after the crew acknowledged / started
    if photo_rules is not None:
        for stop in done:
            if outcome_bucket(stop.outcome) in ("delivered", "partial") and _photo_missing(stop, photo_rules.get(stop.form_template_id) or []):
                hit("photo_missing", stop.so_id)
    order = list(ATTENTION_LABELS)
    return sorted(hits, key=lambda h: order.index(h["code"]))


def attention_flags(facts: BatchFacts, now: datetime, stale_minutes: int, *, events=(), open_issues=(), photo_rules: dict | None = None) -> list[dict]:
    """One chip per code (the first finding of each), in display order. Pure."""
    seen: dict[str, dict] = {}
    for entry in attention_hits(facts, now, stale_minutes, events=events, open_issues=open_issues, photo_rules=photo_rules):
        seen.setdefault(entry["code"], entry)
    return list(seen.values())


def flags_by_stop(hits: list[dict]) -> dict[str, list[str]]:
    """so_id -> attention codes found on that stop (the drawer's per-stop flags)."""
    out: dict[str, list[str]] = defaultdict(list)
    for entry in hits:
        so_id = entry.get("stop_so_id")
        if so_id is not None and entry["code"] not in out[so_id]:
            out[so_id].append(entry["code"])
    return out


# ---- search ---------------------------------------------------------------------------------------------------------------


def search_key(text) -> str:
    """Case-, space- and punctuation-insensitive form: "SO-00123" == "so 00123" == "so00123"."""
    return "".join(ch for ch in str(text or "").casefold() if ch.isalnum())


def matches(needle: str, values) -> bool:
    """`needle` (already search_key'd) is a substring of any value's search_key. An empty needle matches everything."""
    def without_padding(value):
        return re.sub(r"\d+", lambda match: str(int(match.group())), value)
    return not needle or any(needle in search_key(value) or without_padding(needle) in without_padding(search_key(value)) for value in values if value is not None)


# ---- loading ------------------------------------------------------------------------------------------------------------------


def lines_by_order(db: Session, order_ids: list[str]) -> dict[str, list]:
    grouped: dict[str, list] = defaultdict(list)
    if order_ids:
        for line in db.execute(select(SalesOrderLine).where(SalesOrderLine.sales_order_id.in_(order_ids)).order_by(SalesOrderLine.id)).scalars().all():
            grouped[line.sales_order_id].append(line)
    return grouped


def rows_in_range(db: Session, start: date, end: date, staff_id: int | None = None) -> list:
    query = select(SalesOrderHistory).where(SalesOrderHistory.expected_shipment_date >= start, SalesOrderHistory.expected_shipment_date <= end, SalesOrderHistory.assignment_status.in_(VISIBLE_STATUSES))
    if staff_id is not None:
        query = query.where(crew_clause(staff_id))
    rows = db.execute(query).scalars().all()
    return [row for row in rows if staff_id is None or is_crew(row, staff_id)]


def group_by_batch(rows: list) -> dict[str, list]:
    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        grouped[batch_key(row)].append(row)
    return grouped


def load(db: Session, start: date, end: date, *, staff_id: int | None = None) -> dict[str, BatchFacts]:
    """Facts of every batch in [start, end] (Manila shipment dates). With `staff_id`: only batches that person is crew on.
    Without it (Driver Overview): also batches known only from their trip."""
    from services import trip_state  # lazy: trip_state imports this module

    rows = rows_in_range(db, start, end, staff_id)
    grouped = group_by_batch(rows)
    orphan_trips: dict[str, Trip] = {}
    if staff_id is None:
        for trip in db.execute(select(Trip).where(Trip.delivery_date >= start, Trip.delivery_date <= end)).scalars().all():
            if trip.batch_id not in grouped:
                orphan_trips[trip.batch_id] = trip
    batch_ids = list(grouped) + list(orphan_trips)
    data = trip_state.load(db, batch_ids, names=False)
    lines = lines_by_order(db, [row.id for row in rows])
    items = data.handover_items  # read together with the handovers (services/trip_state.load), one query only when a handover exists
    out = {batch_id: facts_for_rows(batch_id, group, lines, data, items_by_handover=items) for batch_id, group in grouped.items()}
    for batch_id, trip in orphan_trips.items():
        out[batch_id] = facts_for_rows(batch_id, [], {}, data, items_by_handover=items, trip_only=trip)
    return out


def load_batch(db: Session, batch_id: str) -> BatchFacts | None:
    """Facts of ONE batch (the Driver Overview trip drawer). Same tables / functions as load(); None for a legacy key or a batch
    with neither assigned rows nor a trip (every order was unassigned before the crew acted)."""
    from services import trip_state  # lazy: trip_state imports this module

    if not batch_id or batch_id.startswith("legacy-"):
        return None
    rows = db.execute(select(SalesOrderHistory).where(SalesOrderHistory.assignment_batch_id == batch_id, SalesOrderHistory.assignment_status.in_(VISIBLE_STATUSES))).scalars().all()
    data = trip_state.load(db, [batch_id], names=False)
    if rows:
        return facts_for_rows(batch_id, list(rows), lines_by_order(db, [row.id for row in rows]), data, items_by_handover=data.handover_items)
    trip = data.trips.get(batch_id)
    if trip is not None:
        return facts_for_rows(batch_id, [], {}, data, items_by_handover=data.handover_items, trip_only=trip)
    return None


# ---- aggregation --------------------------------------------------------------------------------------------------------------


def totals(batches: list[BatchFacts]) -> dict:
    """The numbers every screen prints. A figure that is unknown is None, never 0."""
    packs = [b.packs for b in batches if b.packs is not None]
    return {
        "batches": len(batches),
        "sos": sum(b.so_count for b in batches),
        "kg": round(sum(b.kg for b in batches), 2),
        "kg_complete": bool(batches) and all(b.kg_complete for b in batches),
        "kg_unknown_lines": sum(b.kg_unknown_lines for b in batches),
        "packs": round(sum(packs), 2) if packs else None,
        "acknowledged": sum(b.acknowledged for b in batches),
        "stops_done": sum(b.stops_done for b in batches),
        "delivered": sum(b.delivered for b in batches),
        "partial": sum(b.partial for b in batches),
        "failed": sum(b.failed for b in batches),
    }


def _order_key(b: BatchFacts):
    return (b.assigned_at.timestamp() if b.assigned_at else 0.0, b.batch_id)


def trip_numbers(batches: list[BatchFacts]) -> dict[str, tuple[int, int]]:
    """batch_id -> (trip number, trips that day) for ONE person's batches: Trip 1 / Trip 2 by assignment time, per delivery date.
    Two Assign clicks to the same truck are two real trips; this never merges them."""
    per_day: dict[date | None, list[BatchFacts]] = defaultdict(list)
    for facts in batches:
        per_day[facts.date].append(facts)
    out: dict[str, tuple[int, int]] = {}
    for same_day in per_day.values():
        same_day.sort(key=_order_key)
        for index, facts in enumerate(same_day, 1):
            out[facts.batch_id] = (index, len(same_day))
    return out


def by_driver(batches: dict[str, BatchFacts] | list[BatchFacts]) -> dict[int, list[BatchFacts]]:
    """Batches per PRIMARY driver (helpers are not drivers), oldest first so "Trip 1" is the first assignment of the day."""
    out: dict[int, list[BatchFacts]] = defaultdict(list)
    for facts in (batches.values() if isinstance(batches, dict) else batches):
        if facts.driver_id is not None:
            out[facts.driver_id].append(facts)
    for items in out.values():
        items.sort(key=lambda b: (b.date or date.min, *_order_key(b)))
    return out
