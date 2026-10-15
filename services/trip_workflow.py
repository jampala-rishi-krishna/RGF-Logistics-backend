"""Crew trip actions: acknowledge, start, check-in, check-out, end, handover - plus route / templates / officers reads.
Contract: docs/TRIP_WORKFLOW.md (sections 2-5). No Zoho call anywhere; every action is ONE database commit (state change + its
trip_events audit row), after which trip_events.bump() invalidates the read caches.

Order of checks inside every action (so retries and duplicate taps never fail):
  1. role (only driver / helper act; admin 403)  2. crew membership (not on the batch -> 404)  3. idempotency key replay
  4. "already done by anyone" (natural idempotency, 200 + done_by)  5. state-order checks (409)  6. body validation (422)  7. write.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser
from models.trip import (KIND_ACKNOWLEDGE, KIND_CHECKOUT, InvoiceHandover, InvoiceHandoverItem, SoAcknowledgement, Trip, TripPhoto, TripStop)
from models.user import User
from services import day_model, dispatch_changes, driver_assignments, form_templates, live_sales_order_cache, media_storage, order_types, stop_order, transfer_orders, trip_events, trip_state, warehouses
from services.sales_order_location import address_lines
from services.trip_events import Actor, fail

OUT_OF_ORDER = "out_of_order"


# ---- request bodies ---------------------------------------------------------------------------------------------------


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    idempotency_key: str = Field(min_length=8, max_length=100)
    client_ts: str | None = None
    gps: dict | None = None
    gps_denied_reason: str | None = None


class AcknowledgeBody(ActionBody):
    template_id: int | None = None
    answers: list[dict] = Field(default_factory=list)
    discrepancy_note: str | None = None


class CheckoutBody(ActionBody):
    template_id: int | None = None
    answers: dict[str, Any] = Field(default_factory=dict)


class HandoverItemBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    so_id: str
    handed_over: bool
    note: str | None = None


class HandoverBody(ActionBody):
    officer_user_id: int | None = None  # optional since End trip creates the handover itself (no officer chosen)
    note: str | None = None
    items: list[HandoverItemBody] = Field(default_factory=list)


class AcknowledgeAllBody(ActionBody):
    template_id: int | None = None
    answers: dict[str, list[dict]] = Field(default_factory=dict)  # so_id -> answers as the single acknowledge takes (checklist on only)


# ---- plumbing ---------------------------------------------------------------------------------------------------------


@dataclass
class Duplicate:
    done_by: dict
    response: dict = field(default_factory=dict)  # extra response keys (e.g. the handover of an ended trip)


@dataclass
class Done:
    event_type: str
    so_id: str | None = None
    payload: dict = field(default_factory=dict)
    response: dict = field(default_factory=dict)  # extra response keys, stored with the result (so a replay returns them)
    extra_events: list = field(default_factory=list)  # [(event_type, payload)] written in the same transaction, without the key


@dataclass
class Ctx:
    db: Session
    user: CurrentUser
    batch_id: str
    rows: list
    body: ActionBody
    so_id: str | None
    received_at: datetime
    client_ts: datetime
    gps: dict
    trip: Trip | None
    acks: dict[str, SoAcknowledgement]
    stops: dict[str, TripStop]
    handover: InvoiceHandover | None

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def acked(self) -> int:
        return sum(1 for row in self.rows if row.id in self.acks)

    def row_of(self, so_id: str | None):
        row = next((r for r in self.rows if r.id == so_id), None)
        if row is None:
            dispatch_changes.fail_if_order_removed(self.db, self.batch_id, so_id)  # 409 order_removed (contract 4)
            fail(404, "That order is not part of this assignment.", "not_found")
        return row


def _done_by(db: Session, user_id: int | None, at: datetime | None, role: str | None = None) -> dict:
    found = db.get(User, user_id) if user_id is not None else None
    return {"name": found.full_name if found else "Unknown", "role": role or (found.role if found else None), "at": trip_state.iso(at)}


def _replay(db: Session, key: str | None, batch_id: str) -> dict | None:
    event = trip_events.find_by_key(db, key)
    if event is None:
        return None
    if event.batch_id != batch_id:
        fail(409, "This request id was already used for a different action.", "conflict")
    return {**(event.result or {"ok": True}), "replayed": True}


def _batch(db: Session, user: CurrentUser, batch_id: str) -> dict:
    detail = driver_assignments.batch_detail(db, user.staff_id, batch_id, True)
    if detail is None:
        fail(404, "Assignment not found.", "not_found")
    return detail


def _response(db: Session, user: CurrentUser, batch_id: str, *, already_done: bool, done_by: dict | None) -> dict:
    return {"ok": True, "already_done": already_done, "replayed": False, "done_by": done_by, "batch": _batch(db, user, batch_id)}


def _photo_checker(db: Session, batch_id: str):
    cache: dict[int, bool] = {}

    def check(photo_id: int) -> bool:
        if photo_id not in cache:
            cache[photo_id] = db.execute(select(TripPhoto.id).where(TripPhoto.id == photo_id, TripPhoto.batch_id == batch_id)).first() is not None
        return cache[photo_id]

    return check


def _warehouse_of(name: str | None) -> dict | None:
    key = (name or "").strip().lower()
    if not key:
        return None
    for warehouse_id, warehouse in warehouses.WAREHOUSES.items():
        if warehouse_id == key or warehouse_id in key:
            return warehouse
    return None


def _crew_member(db: Session, user: CurrentUser, batch_id: str) -> list:
    if user.role not in ("driver", "helper"):
        fail(403, "Only the driver and helpers of this trip can do this.", "forbidden")
    if user.staff_id is None:
        fail(403, "This login is not linked to a driver record. Contact dispatch.", "forbidden")
    rows = driver_assignments.rows_for_batch(db, user.staff_id, batch_id)
    if not rows:
        dispatch_changes.fail_if_trip_removed(db, user.staff_id, batch_id)  # 409 trip_removed when dispatch took it away (contract 4)
        fail(404, "Assignment not found.", "not_found")  # same answer for "does not exist" and "not your batch"
    return rows


def _own(rows: list, by_so: dict) -> dict:
    """Workflow rows (acks / stops) of the orders still on the batch: an order dispatch removed keeps its history rows, but they
    must not block or count for the rest of the trip."""
    ids = {row.id for row in rows}
    return {so_id: value for so_id, value in by_so.items() if so_id in ids}


def _ensure_trip(ctx: Ctx) -> Trip:
    if ctx.trip is not None:
        return ctx.trip
    first = ctx.rows[0]
    staff = driver_assignments.staff_directory_cache.get_by_id(first.driver_id) if first.driver_id is not None else None
    warehouse = _warehouse_of((staff or {}).get("warehouse"))
    trip = Trip(
        batch_id=ctx.batch_id, status="pending", delivery_date=first.expected_shipment_date or driver_assignments.manila_today(), vehicle=first.vehicle_id,
        driver_staff_id=first.driver_id, helper_staff_ids=driver_assignments.helper_ids_of(first.driver_id, first.helper_ids),
        warehouse=(warehouse["id"].upper() if warehouse else ((staff or {}).get("warehouse") or None)),
    )
    ctx.db.add(trip)
    try:
        ctx.db.flush()
    except IntegrityError:  # another request created the trip first (nothing else was written yet)
        ctx.db.rollback()
        trip = ctx.db.execute(select(Trip).where(Trip.batch_id == ctx.batch_id)).scalar_one()
    ctx.trip = trip
    return trip


# ---- the runner ---------------------------------------------------------------------------------------------------------


def run_action(db: Session, user: CurrentUser, batch_id: str, body: ActionBody, event_type: str, handler, so_id: str | None = None) -> dict:
    rows = _crew_member(db, user, batch_id)
    replay = _replay(db, body.idempotency_key, batch_id)
    if replay is not None:
        return replay
    received_at = trip_events.now_utc()
    client_ts, _adjusted = trip_events.parse_client_ts(body.client_ts, received_at)
    gps = trip_events.clean_gps(body.gps, body.gps_denied_reason)
    data = trip_state.load(db, [batch_id], names=False)
    trip = data.trips.get(batch_id)
    ctx = Ctx(db=db, user=user, batch_id=batch_id, rows=rows, body=body, so_id=so_id, received_at=received_at, client_ts=client_ts, gps=gps, trip=trip,
              acks=_own(rows, data.acks.get(trip.id, {})) if trip else {}, stops=_own(rows, data.stops.get(trip.id, {})) if trip else {}, handover=data.handovers.get(batch_id))
    try:
        outcome = handler(ctx)
        if isinstance(outcome, Duplicate):
            return {**_response(db, user, batch_id, already_done=True, done_by=outcome.done_by), **outcome.response}
        db.flush()
    except IntegrityError:  # a concurrent request wrote the same thing
        db.rollback()
        replay = trip_events.replay_of(db, body.idempotency_key)
        if replay is not None:
            return replay
        fail(409, "Someone else just updated this trip. Refresh and check the new state.", "conflict")
    result = {**_response(db, user, batch_id, already_done=False, done_by=None), **outcome.response}
    actor = Actor(user_id=user.id, role=user.role, staff_id=user.staff_id, name=user.full_name)
    trip_events.add_event(db, trip_id=ctx.trip.id if ctx.trip else None, batch_id=batch_id, event_type=outcome.event_type, actor=actor, received_at=received_at,
                          client_ts=client_ts, gps=gps, so_id=outcome.so_id or so_id, key=body.idempotency_key, payload=outcome.payload, result=result)
    for extra_type, extra_payload in outcome.extra_events:
        trip_events.add_event(db, trip_id=ctx.trip.id if ctx.trip else None, batch_id=batch_id, event_type=extra_type, actor=actor, received_at=received_at,
                              client_ts=client_ts, gps=gps, payload=extra_payload)
    replay = trip_events.commit_or_replay(db, body.idempotency_key)
    if replay is not None:
        return replay
    trip_events.bump()
    return result


# ---- handlers -------------------------------------------------------------------------------------------------------------


def _acknowledge(ctx: Ctx):
    ctx.row_of(ctx.so_id)
    existing = ctx.acks.get(ctx.so_id)
    if existing is not None:
        return Duplicate(_done_by(ctx.db, existing.by_user_id, existing.acknowledged_at, existing.by_role))
    if ctx.trip is not None and ctx.trip.status != "pending":
        fail(409, "The trip has already started.", OUT_OF_ORDER)
    body: AcknowledgeBody = ctx.body  # type: ignore[assignment]
    template, answers = _checked_answers(ctx, body.template_id, body.answers)
    trip = _ensure_trip(ctx)
    _add_ack(ctx, trip, ctx.so_id, template, answers, body.discrepancy_note)
    return Done("acknowledged", ctx.so_id, {"template_id": template["id"], "template_version": template["version"], "answers": answers, "discrepancy_note": body.discrepancy_note})


def _checked_answers(ctx: Ctx, template_id: int | None, given: list[dict] | None, error_prefix: str = "") -> tuple[dict, list[dict]]:
    """(template, answers to store). Checklist ON (an admin-saved, non-draft template): answers are validated (422 validation).
    Checklist OFF (default): no answers are required; any sent are kept only when they are valid."""
    enabled = form_templates.acknowledge_checklist_enabled(ctx.db)
    template = form_templates.acknowledge_template_for_write(ctx.db, template_id)
    if template is None:
        if template_id is not None:
            fail(422, "That checklist version does not exist.", "validation", [{"key": "template_id", "message": "Unknown checklist version."}])
        fail(409, "The acknowledge checklist template is not configured yet. Contact dispatch.", "not_found")
    if not enabled and not given:
        return template, []
    answers, errors = form_templates.validate_acknowledgement(template, given, media_storage.is_enabled(), _photo_checker(ctx.db, ctx.batch_id))
    if errors:
        if not enabled:
            return template, []
        fail(422, "Please complete the checklist.", "validation", [{**e, "key": f"{error_prefix}{e['key']}"} for e in errors])
    return template, answers


def _add_ack(ctx: Ctx, trip: Trip, so_id: str, template: dict, answers: list[dict], note: str | None = None) -> None:
    ctx.db.add(SoAcknowledgement(
        trip_id=trip.id, batch_id=ctx.batch_id, so_id=so_id, template_id=template["id"], template_version=template["version"], answers=answers,
        discrepancy_note=(note or "").strip() or None, acknowledged_at=ctx.client_ts, received_at=ctx.received_at,
        by_user_id=ctx.user.id, by_role=ctx.user.role, by_staff_id=ctx.user.staff_id, gps=ctx.gps,
    ))


def _start(ctx: Ctx):
    trip = ctx.trip
    if trip is not None and trip.status != "pending":
        return Duplicate(_done_by(ctx.db, trip.started_by_user_id, trip.started_at))
    if ctx.acked < ctx.total:
        fail(409, trip_state.start_blocker(ctx.total, ctx.acked), OUT_OF_ORDER)
    trip = _ensure_trip(ctx)
    order, source = stop_order.order_for_batch(ctx.rows)
    by_id = {row.id: row for row in ctx.rows}
    for sequence, so_id in enumerate(order, 1):
        row = by_id[so_id]
        lat, lng = stop_order.coordinates_of(row)
        ctx.db.add(TripStop(trip_id=trip.id, so_id=so_id, sequence=sequence, so_number=row.salesorder_number or row.id, customer_name=row.customer_name,
                            address_text=", ".join(address_lines(row.shipping_address)), lat=lat, lng=lng, status="pending"))
    trip.status = "on_trip"
    trip.started_at, trip.started_received_at, trip.started_by_user_id, trip.started_gps = ctx.client_ts, ctx.received_at, ctx.user.id, ctx.gps
    trip.stop_order_source = source
    return Done("trip_started", None, {"stop_order_source": source, "sequence": order})


def _not_on_trip(trip: Trip | None, action: str):
    if trip is None or trip.status == "pending":
        fail(409, f"Start the trip before {action}.", OUT_OF_ORDER)
    fail(409, "The trip has already ended.", OUT_OF_ORDER)


def _check_in(ctx: Ctx):
    ctx.row_of(ctx.so_id)
    stop = ctx.stops.get(ctx.so_id)
    if stop is not None and stop.status != "pending":
        return Duplicate(_done_by(ctx.db, stop.checkin_by_user_id, stop.checkin_at))
    if ctx.trip is None or ctx.trip.status != "on_trip":
        _not_on_trip(ctx.trip, "checking in")
    if stop is None:
        fail(409, "This order is not part of the started trip.", OUT_OF_ORDER)
    open_stop = next((s for s in ctx.stops.values() if s.status == "checked_in"), None)
    if open_stop is not None:
        fail(409, f"Check out of {open_stop.so_number or open_stop.so_id} before arriving at another stop.", OUT_OF_ORDER)
    distance = trip_events.distance_between(ctx.gps, stop.lat, stop.lng)
    stop.status = "checked_in"
    stop.checkin_at, stop.checkin_received_at, stop.checkin_by_user_id, stop.checkin_gps = ctx.client_ts, ctx.received_at, ctx.user.id, ctx.gps
    stop.checkin_distance_m = distance
    stop.checkin_far = None if distance is None else distance > trip_events.FAR_THRESHOLD_M
    return Done("checked_in", ctx.so_id, {"distance_m": distance, "far": stop.checkin_far})


def _check_out(ctx: Ctx):
    row = ctx.row_of(ctx.so_id)
    stop = ctx.stops.get(ctx.so_id)
    if stop is not None and stop.status == "checked_out":
        return Duplicate(_done_by(ctx.db, stop.checkout_by_user_id, stop.checkout_at))
    if ctx.trip is None or ctx.trip.status != "on_trip":
        _not_on_trip(ctx.trip, "checking out")
    if stop is None:
        fail(409, "This order is not part of the started trip.", OUT_OF_ORDER)
    if stop.status != "checked_in":
        fail(409, "Check in at this stop first.", OUT_OF_ORDER)
    body: CheckoutBody = ctx.body  # type: ignore[assignment]
    template = form_templates.load_for_kind(ctx.db, KIND_CHECKOUT, body.template_id)
    if template is None:
        fail(422, "That delivery form version does not exist.", "validation", [{"key": "template_id", "message": "Unknown form version."}])
    crew = trip_state.crew_of(ctx.rows)
    prefill = driver_assignments._checkout_prefill(template, row, ", ".join(address_lines(row.shipping_address)), crew)
    kind = transfer_orders.order_type_of(row)
    is_transfer = order_types.is_reference_type(kind)  # TO or PO: no invoice, the order number is the reference
    sent = dict(body.answers or {})
    if is_transfer:
        sent.pop("invoice_dr_number", None)  # contract 5: whatever the phone sent is ignored for a TO
    answers, errors = form_templates.validate_checkout(template, sent, media_storage.is_enabled(), _photo_checker(ctx.db, ctx.batch_id), prefill, skip_keys=frozenset({"invoice_dr_number"}) if is_transfer else frozenset())
    if errors:
        fail(422, "Please complete the delivery form.", "validation", errors)
    if is_transfer:
        # No invoice for a transfer order: the server stores its TO# as the reference, so the timeline, handover, hard copies and CSV show it.
        answers.append({"key": "invoice_dr_number", "label": f"Reference ({kind}#)", "type": "text", "value": row.salesorder_number or row.id})
    values = {a["key"]: a["value"] for a in answers}
    stop.status = "checked_out"
    stop.checkout_at, stop.checkout_received_at, stop.checkout_by_user_id, stop.checkout_gps = ctx.client_ts, ctx.received_at, ctx.user.id, ctx.gps
    stop.outcome = values.get("outcome") or "Delivered"  # a template without an outcome question records every stop as delivered
    stop.reason = values.get("reason")
    stop.form_template_id, stop.form_template_version, stop.form_answers = template["id"], template["version"], answers
    if is_transfer and day_model.outcome_bucket(stop.outcome) in ("delivered", "partial") and row.assignment_status == "assigned":
        # Zoho never reports a transfer / purchase order "delivered" (Fleet's sync does that for sales orders), so the crew's check-out completes it.
        row.assignment_status, row.completed_at = "completed", ctx.received_at
        live_sales_order_cache.set_assignment(row.id, assignment_status="completed", completed_at=ctx.received_at)
    return Done("checked_out", ctx.so_id, {"template_id": template["id"], "template_version": template["version"], "outcome": stop.outcome, "reason": stop.reason})


def _end(ctx: Ctx):
    trip = ctx.trip
    if trip is not None and trip.status not in ("pending", "on_trip"):
        return Duplicate(_done_by(ctx.db, trip.ended_by_user_id, trip.ended_at), {"handover": handover_payload(ctx.db, ctx.handover, ctx.rows)})
    if trip is None or trip.status != "on_trip":
        fail(409, "Start the trip before ending it.", OUT_OF_ORDER)
    remaining = sum(1 for s in ctx.stops.values() if s.status != "checked_out") + sum(1 for row in ctx.rows if row.id not in ctx.stops)
    if remaining:
        fail(409, trip_state.end_blocker(remaining), OUT_OF_ORDER)
    warehouse = _warehouse_of(trip.warehouse)
    distance = trip_events.distance_between(ctx.gps, warehouse["lat"], warehouse["lng"]) if warehouse else None
    trip.status = "returned"
    trip.ended_at, trip.ended_received_at, trip.ended_by_user_id, trip.ended_gps, trip.end_distance_m = ctx.client_ts, ctx.received_at, ctx.user.id, ctx.gps, distance
    # Contract 2: End trip hands over every invoice / DR (TO: its TO#) automatically, in the same transaction; the officer validates.
    handover, items = _create_handover(ctx, trip, officer_user_id=None, note=None, handed={row.id: (True, None) for row in ctx.rows})
    return Done("trip_ended", None, {"end_distance_m": distance, "handover_id": handover.id}, response={"handover": handover_payload(ctx.db, handover, ctx.rows, items)},
                extra_events=[("handover_submitted", {"handover_id": handover.id, "automatic": True, "items": [{"so_id": i.so_id, "invoice_no": i.invoice_no} for i in items]})])


def _create_handover(ctx: Ctx, trip: Trip, *, officer_user_id: int | None, note: str | None, handed: dict[str, tuple[bool, str | None]]):
    handover = InvoiceHandover(trip_id=trip.id, batch_id=ctx.batch_id, status="pending", submitted_by_user_id=ctx.user.id, submitted_at=ctx.client_ts,
                               submitted_received_at=ctx.received_at, submitted_gps=ctx.gps, officer_user_id=officer_user_id, note=note)
    ctx.db.add(handover)
    ctx.db.flush()
    by_id = {row.id: row for row in ctx.rows}
    items = []
    for so_id, (handed_over, crew_note) in handed.items():
        row = by_id[so_id]
        item = InvoiceHandoverItem(handover_id=handover.id, so_id=so_id, so_number=row.salesorder_number or row.id, customer_name=row.customer_name, handed_over=handed_over,
                                   crew_note=crew_note, officer_status="pending", invoice_no=_invoice_no(row, ctx.stops.get(so_id)))
        ctx.db.add(item)
        items.append(item)
    trip.status = "handover_submitted"
    ctx.handover = handover
    return handover, items


def _invoice_no(row, stop) -> str | None:
    """Invoice / DR number captured at check-out; a transfer order's reference is always its TO#."""
    if order_types.is_reference_type(transfer_orders.order_type_of(row)):
        return row.salesorder_number or row.id
    return trip_state.invoice_no_of(stop)


def handover_payload(db: Session, handover: InvoiceHandover | None, rows: list, items: list | None = None) -> dict | None:
    """Contract 2: {"id","status","items":[{"so_id","so_number","order_type","invoice_no","customer_name","status"}]}."""
    if handover is None:
        return None
    if items is None:
        items = db.execute(select(InvoiceHandoverItem).where(InvoiceHandoverItem.handover_id == handover.id).order_by(InvoiceHandoverItem.id)).scalars().all()
    by_id = {row.id: row for row in rows}
    return {"id": handover.id, "status": handover.status, "items": [
        {"so_id": i.so_id, "so_number": i.so_number, "order_type": transfer_orders.order_type_of(by_id[i.so_id]) if i.so_id in by_id else order_types.order_type_of(i.so_id, i.so_number), "invoice_no": i.invoice_no,
         "customer_name": i.customer_name, "status": i.officer_status} for i in items]}


def _handover(ctx: Ctx):
    existing = ctx.handover
    if existing is not None:  # End trip already created it (contract 2): return it, never a duplicate
        return Duplicate(_done_by(ctx.db, existing.submitted_by_user_id, existing.submitted_at), {"handover": handover_payload(ctx.db, existing, ctx.rows)})
    trip = ctx.trip
    if trip is None or trip.status != "returned":
        fail(409, "End the trip before handing over invoices.", OUT_OF_ORDER)
    body: HandoverBody = ctx.body  # type: ignore[assignment]
    if body.officer_user_id is None and not body.items:
        # A trip ended before automatic handovers existed, sent by the new app (no form): hand over every SO, no officer chosen.
        handover, items = _create_handover(ctx, trip, officer_user_id=None, note=(body.note or "").strip() or None, handed={row.id: (True, None) for row in ctx.rows})
        return Done("handover_submitted", None, {"officer_user_id": None, "automatic": True, "note": body.note}, response={"handover": handover_payload(ctx.db, handover, ctx.rows, items)})
    errors: list[dict] = []
    officer = ctx.db.get(User, body.officer_user_id) if body.officer_user_id is not None else None
    if body.officer_user_id is not None and (officer is None or officer.role != "logistics_officer" or officer.status not in (None, "active")):
        errors.append({"key": "officer_user_id", "message": "Choose an active logistics officer."})
    by_id = {row.id: row for row in ctx.rows}
    seen: dict[str, HandoverItemBody] = {}
    for item in body.items:
        if item.so_id not in by_id:
            errors.append({"key": item.so_id, "message": "That order is not part of this trip."})
        elif item.so_id in seen:
            errors.append({"key": item.so_id, "message": "This order is listed twice."})
        else:
            seen[item.so_id] = item
            if not item.handed_over and not (item.note or "").strip():
                errors.append({"key": item.so_id, "message": f"Add a note for {by_id[item.so_id].salesorder_number or item.so_id}: why the invoice was not handed over."})
    for so_id, row in by_id.items():
        if so_id not in seen:
            errors.append({"key": so_id, "message": f"List {row.salesorder_number or so_id} in the handover."})
    if errors:
        fail(422, "Please check the invoice handover.", "validation", errors)
    handover, items = _create_handover(ctx, trip, officer_user_id=body.officer_user_id, note=(body.note or "").strip() or None,
                                       handed={so_id: (item.handed_over, (item.note or "").strip() or None) for so_id, item in seen.items()})
    return Done("handover_submitted", None, {"officer_user_id": body.officer_user_id, "items": [{"so_id": i.so_id, "handed_over": i.handed_over, "note": i.note} for i in body.items], "note": body.note},
                response={"handover": handover_payload(ctx.db, handover, ctx.rows, items)})


def acknowledge(db: Session, user: CurrentUser, batch_id: str, so_id: str, body: AcknowledgeBody) -> dict:
    return run_action(db, user, batch_id, body, "acknowledged", _acknowledge, so_id)


def acknowledge_all(db: Session, user: CurrentUser, batch_id: str, body: AcknowledgeAllBody) -> dict:
    """Contract 1: one tap acknowledges every order (SO and TO) of the batch not yet acknowledged, in ONE transaction: per order the
    same so_acknowledgements row + "acknowledged" trip_events row as the single endpoint (who, when, GPS). The first event carries the
    idempotency key and the stored response, so a replay of the key returns the first result and writes nothing."""
    rows = _crew_member(db, user, batch_id)
    replay = _replay(db, body.idempotency_key, batch_id)
    if replay is not None:
        return replay
    received_at = trip_events.now_utc()
    client_ts, _adjusted = trip_events.parse_client_ts(body.client_ts, received_at)
    gps = trip_events.clean_gps(body.gps, body.gps_denied_reason)
    data = trip_state.load(db, [batch_id], names=False)
    trip = data.trips.get(batch_id)
    ctx = Ctx(db=db, user=user, batch_id=batch_id, rows=rows, body=body, so_id=None, received_at=received_at, client_ts=client_ts, gps=gps, trip=trip,
              acks=_own(rows, data.acks.get(trip.id, {})) if trip else {}, stops=_own(rows, data.stops.get(trip.id, {})) if trip else {}, handover=data.handovers.get(batch_id))
    ordered = sorted(rows, key=driver_assignments._sort_key)
    already = [row.id for row in ordered if row.id in ctx.acks]
    todo = [row for row in ordered if row.id not in ctx.acks]
    if not todo:  # nothing to write (natural idempotency): same answer every time
        return {"ok": True, "acknowledged": [], "already_acknowledged": already, "replayed": False, "already_done": True, "done_by": None, "batch": _batch(db, user, batch_id)}
    if trip is not None and trip.status != "pending":
        fail(409, "The trip has already started.", OUT_OF_ORDER)
    checked: list[tuple[str, dict, list[dict]]] = []
    errors: list[dict] = []
    for row in todo:
        try:
            template, answers = _checked_answers(ctx, body.template_id, body.answers.get(row.id), error_prefix=f"{row.id}:")
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            if detail.get("code") != "validation":
                raise
            errors.extend(detail.get("errors") or [{"key": row.id, "message": detail.get("message")}])
            continue
        checked.append((row.id, template, answers))
    if errors:
        fail(422, "Please complete the checklist for every order.", "validation", errors)
    try:
        trip = _ensure_trip(ctx)
        for so_id, template, answers in checked:
            _add_ack(ctx, trip, so_id, template, answers)
        db.flush()
    except IntegrityError:  # a crew member acknowledged one of these at the same moment
        db.rollback()
        replay = trip_events.replay_of(db, body.idempotency_key)
        if replay is not None:
            return replay
        fail(409, "Someone else just updated this trip. Refresh and check the new state.", "conflict")
    done = [so_id for so_id, _, _ in checked]
    result = {"ok": True, "acknowledged": done, "already_acknowledged": already, "replayed": False, "already_done": False, "done_by": None, "batch": _batch(db, user, batch_id)}
    actor = Actor(user_id=user.id, role=user.role, staff_id=user.staff_id, name=user.full_name)
    for index, (so_id, template, answers) in enumerate(checked):
        first = index == 0
        trip_events.add_event(db, trip_id=trip.id, batch_id=batch_id, event_type="acknowledged", actor=actor, received_at=received_at, client_ts=client_ts, gps=gps, so_id=so_id,
                              key=body.idempotency_key if first else None, payload={"template_id": template["id"], "template_version": template["version"], "answers": answers, "bulk": True},
                              result=result if first else None)
    replay = trip_events.commit_or_replay(db, body.idempotency_key)
    if replay is not None:
        return replay
    trip_events.bump()
    return result


def start(db: Session, user: CurrentUser, batch_id: str, body: ActionBody) -> dict:
    return run_action(db, user, batch_id, body, "trip_started", _start)


def check_in(db: Session, user: CurrentUser, batch_id: str, so_id: str, body: ActionBody) -> dict:
    return run_action(db, user, batch_id, body, "checked_in", _check_in, so_id)


def check_out(db: Session, user: CurrentUser, batch_id: str, so_id: str, body: CheckoutBody) -> dict:
    result = run_action(db, user, batch_id, body, "checked_out", _check_out, so_id)
    if not result.get("replayed") and not result.get("already_done"):
        media_storage.after_checkout(db, batch_id, so_id)  # committed: best-effort Drive name / description update, off the request (never raises)
    return result


def end(db: Session, user: CurrentUser, batch_id: str, body: ActionBody) -> dict:
    return run_action(db, user, batch_id, body, "trip_ended", _end)


def handover(db: Session, user: CurrentUser, batch_id: str, body: HandoverBody) -> dict:
    return run_action(db, user, batch_id, body, "handover_submitted", _handover)


# ---- reads ----------------------------------------------------------------------------------------------------------------


def templates(db: Session) -> dict:
    payload = form_templates.templates_payload(db, media_storage.is_enabled())
    if payload[KIND_ACKNOWLEDGE] is None or payload[KIND_CHECKOUT] is None:
        fail(404, "The checklist / delivery form templates are not configured yet.", "not_found")
    return payload


def officers(db: Session) -> list[dict]:
    users = db.execute(select(User).where(User.role == "logistics_officer").order_by(User.full_name, User.id)).scalars().all()
    return [{"id": u.id, "name": u.full_name} for u in users if u.status in (None, "active")]


MAX_WAYPOINTS = 9
MAPS_DIR = "https://www.google.com/maps/dir/?api=1"


def _token(lat, lng, text: str | None) -> str:
    if lat is not None and lng is not None:
        return f"{lat},{lng}"
    return urllib.parse.quote((text or "").strip(), safe=",")


def _directions_url(origin: str | None, destination: str, waypoints: list[str]) -> str:
    url = MAPS_DIR + (f"&origin={origin}" if origin else "") + f"&destination={destination}"
    if waypoints:
        url += "&waypoints=" + "|".join(waypoints)
    return url + "&travelmode=driving"


def route(db: Session, user: CurrentUser, batch_id: str, staff_id: int) -> dict:
    rows = driver_assignments.rows_for_batch(db, staff_id, batch_id)
    if not rows:
        fail(404, "Assignment not found.", "not_found")
    data = trip_state.load(db, [batch_id], names=False)
    trip = data.trips.get(batch_id)
    frozen = data.stops.get(trip.id) if trip else None
    items: list[dict] = []
    if frozen:
        source = trip.stop_order_source or "assignment"
        for stop in sorted(frozen.values(), key=lambda s: s.sequence):
            items.append({"so_id": stop.so_id, "so_number": stop.so_number, "customer_name": stop.customer_name, "address_text": stop.address_text,
                          "sequence": stop.sequence, "status": stop.status, "lat": stop.lat, "lng": stop.lng})
    else:
        order, source = stop_order.order_for_batch(rows)
        by_id = {row.id: row for row in rows}
        for sequence, so_id in enumerate(order, 1):
            row = by_id[so_id]
            lat, lng = stop_order.coordinates_of(row)
            items.append({"so_id": so_id, "so_number": row.salesorder_number or row.id, "customer_name": row.customer_name,
                          "address_text": ", ".join(address_lines(row.shipping_address)), "sequence": sequence, "status": "pending", "lat": lat, "lng": lng})
    staff = driver_assignments.staff_directory_cache.get_by_id(rows[0].driver_id) if rows[0].driver_id is not None else None
    warehouse = _warehouse_of((trip.warehouse if trip else None) or (staff or {}).get("warehouse"))
    origin = {"name": None, "address": None, "lat": None, "lng": None}
    warning = None
    if warehouse:
        origin = {"name": warehouse["name"], "address": warehouse["address"], "lat": warehouse["lat"], "lng": warehouse["lng"]}
    else:
        warning = "The warehouse of this driver is unknown, so directions start from your current location."
    tokens = []
    for item in items:
        token = _token(item["lat"], item["lng"], item["address_text"] or item["customer_name"] or item["so_number"])
        tokens.append(token)
        item["navigate_url"] = f"{MAPS_DIR}&destination={token}&travelmode=driving"
    links: list[dict] = []
    if tokens:
        chunks = [tokens[i:i + MAX_WAYPOINTS + 1] for i in range(0, len(tokens), MAX_WAYPOINTS + 1)]
        start_token = f"{origin['lat']},{origin['lng']}" if origin["lat"] is not None else None
        for index, chunk in enumerate(chunks):
            links.append({"label": "Full route" if len(chunks) == 1 else f"Part {index + 1} of {len(chunks)}",
                          "url": _directions_url(start_token if index == 0 else chunks[index - 1][-1], chunk[-1], chunk[:-1])})
    return {"source": source, "origin": origin, "stops": items, "maps_links": links, "warning": warning}
