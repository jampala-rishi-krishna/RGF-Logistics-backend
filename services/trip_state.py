"""Derived trip state for the driver app payloads: `trip` + `crew` per batch and `workflow` per order (docs/TRIP_WORKFLOW.md s.5).

One batched query set per request (trips, acknowledgements, stops, handovers, and - only where names are needed - users),
never one set per batch. Read-only: nothing here writes.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.trip import InvoiceHandover, InvoiceHandoverItem, SoAcknowledgement, Trip, TripStop
from models.user import User
from services import day_model, staff_directory_cache
from services.staff_roles import helper_ids_of


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


# ---- messages shared with services/trip_workflow.py --------------------------------------------------------------------


def start_blocker(total: int, acknowledged: int) -> str:
    return f"Acknowledge all {total} SOs to start the trip ({max(total - acknowledged, 0)} left)."


def end_blocker(remaining: int) -> str:
    return "1 stop is not checked out yet." if remaining == 1 else f"{remaining} stops are not checked out yet."


READ_ONLY = "Read-only view: only the driver and helpers can act on a trip."


# ---- batched loading ------------------------------------------------------------------------------------------------


class TripData:
    def __init__(self):
        self.trips: dict[str, Trip] = {}
        self.acks: dict[int, dict[str, SoAcknowledgement]] = defaultdict(dict)
        self.stops: dict[int, dict[str, TripStop]] = defaultdict(dict)
        self.handovers: dict[str, InvoiceHandover] = {}
        self.handover_items: dict[int, list] = defaultdict(list)  # handover id -> InvoiceHandoverItem rows (officer status per invoice)
        self.users: dict[int, dict] = {}

    def user(self, user_id: int | None, role: str | None = None) -> dict | None:
        if user_id is None:
            return None
        found = self.users.get(user_id) or {}
        return {"user_id": user_id, "name": found.get("name") or f"User #{user_id}", "role": role or found.get("role")}


def load(db: Session, batch_ids: list[str], *, names: bool) -> TripData:
    """Trips of `batch_ids` + their acknowledgements, stops and handovers (4 queries), the handovers' invoice items (1 query, only
    when a handover exists), plus the users that need a name (1 query: everyone who acted when names=True, otherwise only the
    receiving officers)."""
    data = TripData()
    if not batch_ids:
        return data
    trips = db.execute(select(Trip).where(Trip.batch_id.in_(batch_ids))).scalars().all()
    data.trips = {trip.batch_id: trip for trip in trips}
    trip_ids = [trip.id for trip in trips]
    user_ids: set[int] = set()
    if trip_ids:
        for ack in db.execute(select(SoAcknowledgement).where(SoAcknowledgement.trip_id.in_(trip_ids))).scalars().all():
            data.acks[ack.trip_id][ack.so_id] = ack
            user_ids.add(ack.by_user_id)
        for stop in db.execute(select(TripStop).where(TripStop.trip_id.in_(trip_ids))).scalars().all():
            data.stops[stop.trip_id][stop.so_id] = stop
            user_ids.update(u for u in (stop.checkin_by_user_id, stop.checkout_by_user_id) if u is not None)
        for handover in db.execute(select(InvoiceHandover).where(InvoiceHandover.trip_id.in_(trip_ids))).scalars().all():
            data.handovers[handover.batch_id] = handover
            user_ids.update(u for u in (handover.officer_user_id, handover.submitted_by_user_id) if u is not None)
        if data.handovers:  # only trips past End trip: the officer's status per invoice (driver Return step, Driver Overview)
            for item in db.execute(select(InvoiceHandoverItem).where(InvoiceHandoverItem.handover_id.in_([h.id for h in data.handovers.values()])).order_by(InvoiceHandoverItem.id)).scalars().all():
                data.handover_items[item.handover_id].append(item)
        for trip in trips:
            user_ids.update(u for u in (trip.started_by_user_id, trip.ended_by_user_id) if u is not None)
    if not names:
        user_ids = {h.officer_user_id for h in data.handovers.values() if h.officer_user_id is not None}
    if user_ids:
        for row in db.execute(select(User.id, User.full_name, User.role).where(User.id.in_(user_ids))).all():
            data.users[row.id] = {"name": row.full_name, "role": row.role}
    return data


def invoice_no_of(stop) -> str | None:
    """The Invoice / DR number typed at check-out (form item `invoice_dr_number`), from the stop's stored answers."""
    for answer in (stop.form_answers if stop is not None else None) or []:
        if isinstance(answer, dict) and answer.get("key") == "invoice_dr_number" and answer.get("value") not in (None, ""):
            return str(answer["value"])
    return None


# ---- crew ---------------------------------------------------------------------------------------------------------------


def _person(staff_id: int, role: str) -> dict:
    staff = staff_directory_cache.get_by_id(staff_id) or {}
    return {"id": staff_id, "name": staff.get("name") or f"Staff #{staff_id}", "role": role, "phone": staff.get("phone") or None}


def crew_of(rows: list) -> list[dict]:
    """Driver first, then the helpers (set difference, never positional)."""
    first = rows[0]
    crew = [_person(first.driver_id, "driver")] if first.driver_id is not None else []
    crew += [_person(hid, "helper") for hid in helper_ids_of(first.driver_id, first.helper_ids)]
    return crew


# ---- trip summary ---------------------------------------------------------------------------------------------------------


def summarize(batch_id: str, rows: list, data: TripData, *, can_act: bool, order_source_optimized: bool) -> tuple[dict, dict]:
    """(`trip` payload, internal facts). Batches with no trip row are state `pending`."""
    total = len(rows)
    trip = data.trips.get(batch_id)
    status = trip.status if trip else "pending"
    own = {row.id for row in rows}  # an order dispatch removed keeps its history rows; they do not count or block here
    acks = {k: v for k, v in data.acks.get(trip.id, {}).items() if k in own} if trip else {}
    stops = {k: v for k, v in data.stops.get(trip.id, {}).items() if k in own} if trip else {}
    prog = day_model.progress([row.id for row in rows], trip, acks, stops)  # the ONE counting implementation
    acknowledged, stops_done = prog["acknowledged"], prog["stops_done"]
    current = next((stop for stop in stops.values() if stop.status == "checked_in"), None)
    handover = data.handovers.get(batch_id)

    chip, label, next_action, blocker = "to_acknowledge", "To acknowledge", "acknowledge", None
    can_start = can_end = can_handover = False
    if status == "pending":
        if acknowledged < total:
            blocker = start_blocker(total, acknowledged)
        else:
            chip, label, next_action, can_start = "ready", "Ready to start", "start", True
    elif status == "on_trip":
        chip, label = "on_trip", f"On trip · stop {min(stops_done + 1, total)} of {total}"
        if current is not None:
            next_action = "check_out"
        elif stops_done < total:
            next_action = "check_in"
        else:
            next_action, can_end = "end", True
        if not can_end:
            blocker = end_blocker(total - stops_done)
    elif status == "returned":
        chip, label, next_action, can_handover = "returned", "Back - hand over invoices", "handover", True
    elif status == "handover_submitted":
        chip, label, next_action = "handover_pending", "Handover pending", "wait_officer"
    else:  # closed
        chip, label, next_action = "closed", "Closed", "none"

    if not can_act:
        can_start = can_end = can_handover = False
        blocker = READ_ONLY
    officer = None
    if handover is not None and handover.officer_user_id is not None:
        person = data.user(handover.officer_user_id)
        officer = {"id": handover.officer_user_id, "name": person["name"]}
    summary = {
        "status": status, "chip": chip, "chip_label": label, "acknowledged": acknowledged, "total": total, "stops_done": stops_done,
        "current_stop_so_id": current.so_id if current else None, "started_at": iso(trip.started_at) if trip else None, "ended_at": iso(trip.ended_at) if trip else None,
        "next_action": next_action, "can_start": can_start, "can_end": can_end, "can_submit_handover": can_handover, "blocker": blocker,
        "handover_status": handover.status if handover else None, "officer": officer,
    }
    started = trip is not None and (trip.status != "pending" or bool(stops))
    return summary, {"trip": trip, "acks": acks, "stops": stops, "started": started, "optimized": order_source_optimized}


# ---- per-order workflow ------------------------------------------------------------------------------------------------------


def order_workflow(row, facts: dict, trip_summary: dict, data: TripData, *, can_act: bool, so_numbers: dict[str, str]) -> dict:
    ack = facts["acks"].get(row.id)
    stop = facts["stops"].get(row.id)
    status = trip_summary["status"]
    current_id = trip_summary["current_stop_so_id"]
    can_ack = can_in = can_out = False
    blocker = None
    if status == "pending":
        if ack is None:
            can_ack = True
        elif trip_summary["acknowledged"] < trip_summary["total"]:
            blocker = "Waiting for the other SOs to be acknowledged."
        else:
            blocker = "Start the trip first."
    elif status == "on_trip" and stop is not None:
        if stop.status == "pending":
            if current_id is not None:
                blocker = f"Check out of {so_numbers.get(current_id, 'the current stop')} first."
            else:
                can_in = True
        elif stop.status == "checked_in":
            can_out = True
    if not can_act:
        can_ack = can_in = can_out = False
        blocker = READ_ONLY
    return {
        "ack": None if ack is None else {"at": iso(ack.acknowledged_at), "by": data.user(ack.by_user_id, ack.by_role), "template_version": ack.template_version},
        "stop": None if stop is None else {
            "sequence": stop.sequence, "status": stop.status, "checkin_at": iso(stop.checkin_at), "checkin_by": data.user(stop.checkin_by_user_id),
            "checkin_distance_m": stop.checkin_distance_m, "checkin_far": stop.checkin_far, "checkout_at": iso(stop.checkout_at),
            "checkout_by": data.user(stop.checkout_by_user_id), "outcome": stop.outcome, "reason": stop.reason, "invoice_no": invoice_no_of(stop),
            # proof photos stored with the check-out answers: id + caption data (SO, client time, result); no extra read
            "photos": day_model.stop_photos(stop, so_numbers.get(row.id) or row.salesorder_number or row.id),
        },
        "can_acknowledge": can_ack, "can_check_in": can_in, "can_check_out": can_out, "blocker": blocker,
    }
