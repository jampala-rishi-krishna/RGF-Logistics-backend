"""THE read model of a day's assignments. Everything that shows "how many SOs / kg / packs / batches / acknowledged / delivered"
(driver app Today + History, Driver Overview KPIs + board, the reconciliation check) goes through `facts_for_rows` and `totals`
below, so the screens cannot disagree: there is no second implementation to drift.

Identity of a batch = `assignment_batch_id` (one Assign click = one truck). Two batches of the same vehicle are never merged.
Rows saved before that column existed get a stable `legacy-...` key (same truck + same assigned_at).

Reads Neon (sales_orders, sales_order_lines, trips and their children) only; never Zoho. Read-only: nothing here writes.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session

from models.sales_order_history import SalesOrderHistory
from models.sales_order_lines import SalesOrderLine
from models.trip import InvoiceHandoverItem, Trip
from services.pack_units import pack_total
from services.staff_roles import helper_ids_of

# Everything except never-assigned / unassigned / released orders is part of a driver's day or history.
VISIBLE_STATUSES = ("assigned", "manifested", "completed", "delivered")
OUTCOME_BUCKET = {"delivered": "delivered", "partially delivered": "partial", "failed": "failed", "returned": "failed", "failed / returned": "failed"}


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
    items: dict[int, list] = defaultdict(list)
    if data.handovers:
        for item in db.execute(select(InvoiceHandoverItem).where(InvoiceHandoverItem.handover_id.in_([h.id for h in data.handovers.values()])).order_by(InvoiceHandoverItem.id)).scalars().all():
            items[item.handover_id].append(item)
    out = {batch_id: facts_for_rows(batch_id, group, lines, data, items_by_handover=items) for batch_id, group in grouped.items()}
    for batch_id, trip in orphan_trips.items():
        out[batch_id] = facts_for_rows(batch_id, [], {}, data, items_by_handover=items, trip_only=trip)
    return out


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
