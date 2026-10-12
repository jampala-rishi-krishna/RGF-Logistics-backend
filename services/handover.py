"""Officer side of the invoice handover: queue, validation, later resolution, outstanding invoices, history.
Contract: docs/TRIP_WORKFLOW.md section 6. Pure Neon + memory (staff names); never Zoho.

Every write is ONE transaction: the state change + its append-only trip_events rows, committed once (services/trip_events).
The idempotency key is stored on the first event of the action together with the response, so a retry replays it.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models.trip import InvoiceHandover, InvoiceHandoverItem, Trip, TripEvent, TripStop
from services import staff_directory_cache, trip_events
from services.trip_events import Actor, fail
from services.trip_timeline import aware, iso, staff_name, users_by_id
from services.staff_roles import helper_ids_of

ITEM_STATUSES = ("received", "missing", "discrepancy")
NOTE_REQUIRED = ("missing", "discrepancy")
LIST_STATUSES = ("pending", "all", "closed")


def _text(value) -> str | None:
    value = (value or "").strip() if isinstance(value, str) else value
    return value or None


# ---- shaping --------------------------------------------------------------------------------------------------------


def _totals(items: list[InvoiceHandoverItem]) -> dict:
    count = lambda status: sum(1 for i in items if i.officer_status == status)  # noqa: E731
    return {"invoices": len(items), "handed_over": sum(1 for i in items if i.handed_over), "not_handed_over": sum(1 for i in items if not i.handed_over),
            "received": count("received"), "missing": count("missing"), "discrepancy": count("discrepancy"), "pending": count("pending")}


def _rows(db: Session, handovers: list[InvoiceHandover], viewer_user_id: int | None) -> list[dict]:
    if not handovers:
        return []
    trips = {t.id: t for t in db.execute(select(Trip).where(Trip.id.in_([h.trip_id for h in handovers]))).scalars().all()}
    items: dict[int, list[InvoiceHandoverItem]] = {}
    for item in db.execute(select(InvoiceHandoverItem).where(InvoiceHandoverItem.handover_id.in_([h.id for h in handovers])).order_by(InvoiceHandoverItem.id)).scalars().all():
        items.setdefault(item.handover_id, []).append(item)
    users = users_by_id(db, [h.officer_user_id for h in handovers])
    out = []
    for h in handovers:
        trip = trips.get(h.trip_id)
        driver_id = trip.driver_staff_id if trip else None
        officer = users.get(h.officer_user_id) if h.officer_user_id is not None else None
        out.append({
            "handover_id": h.id, "batch_id": h.batch_id, "delivery_date": trip.delivery_date.isoformat() if trip and trip.delivery_date else None, "vehicle": trip.vehicle if trip else None,
            "driver": {"id": driver_id, "name": staff_name(driver_id)},
            "helpers": [{"id": i, "name": staff_name(i)} for i in helper_ids_of(driver_id, trip.helper_staff_ids if trip else None)],
            "submitted_at": iso(h.submitted_at), "officer": {"id": officer["user_id"], "name": officer["name"]} if officer else None,
            "for_me": viewer_user_id is not None and h.officer_user_id == viewer_user_id, "status": h.status, "note": h.note,
            "officer_acknowledged_at": iso(h.officer_acknowledged_at), "totals": _totals(items.get(h.id, [])), "_items": items.get(h.id, []),
        })
    return out


def _item_payload(item: InvoiceHandoverItem, users: dict[int, dict]) -> dict:
    return {"item_id": item.id, "so_id": item.so_id, "so_number": item.so_number, "customer_name": item.customer_name, "handed_over": item.handed_over, "crew_note": item.crew_note,
            "officer_status": item.officer_status, "officer_note": item.officer_note, "checked_at": iso(item.officer_checked_at), "checked_by": users.get(item.officer_user_id) if item.officer_user_id is not None else None,
            "resolved_at": iso(item.resolved_at)}


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "_items"}


def _with_items(db: Session, rows: list[dict]) -> list[dict]:
    users = users_by_id(db, [i.officer_user_id for r in rows for i in r["_items"]])
    return [{**_public(r), "items": [_item_payload(i, users) for i in r["_items"]]} for r in rows]


# ---- reads ----------------------------------------------------------------------------------------------------------


def _filtered(db: Session, *, statuses: tuple[str, ...] | None, q: str | None, date_from: date | None, date_to: date | None, driver_id: int | None) -> list[InvoiceHandover]:
    stmt = select(InvoiceHandover).join(Trip, Trip.id == InvoiceHandover.trip_id)
    if statuses:
        stmt = stmt.where(InvoiceHandover.status.in_(statuses))
    if date_from:
        stmt = stmt.where(Trip.delivery_date >= date_from)
    if date_to:
        stmt = stmt.where(Trip.delivery_date <= date_to)
    if driver_id is not None:
        stmt = stmt.where(Trip.driver_staff_id == driver_id)
    q = (q or "").strip()
    if q:
        needle = q.lower()
        conditions = [InvoiceHandover.id.in_(select(InvoiceHandoverItem.handover_id).where(func.lower(InvoiceHandoverItem.so_number).contains(needle, autoescape=True)))]
        named = [s["id"] for s in staff_directory_cache.all_staff() if s.get("id") is not None and needle in str(s.get("name") or "").lower()]
        if named:
            conditions.append(Trip.driver_staff_id.in_(named))
        try:
            conditions.append(Trip.delivery_date == date.fromisoformat(q))
        except ValueError:
            pass
        stmt = stmt.where(or_(*conditions))
    return list(db.execute(stmt.order_by(InvoiceHandover.submitted_at.desc(), InvoiceHandover.id.desc())).scalars().all())


def list_handovers(db: Session, viewer_user_id: int, *, status: str = "pending", q=None, date_from=None, date_to=None, driver_id=None) -> list[dict]:
    if status not in LIST_STATUSES:
        fail(422, "status must be pending, all or closed.", "validation")
    statuses = {"pending": ("pending", "acknowledged"), "closed": ("closed",), "all": None}[status]
    return [_public(r) for r in _rows(db, _filtered(db, statuses=statuses, q=q, date_from=date_from, date_to=date_to, driver_id=driver_id), viewer_user_id)]


def history(db: Session, viewer_user_id: int, *, q=None, date_from=None, date_to=None, driver_id=None) -> list[dict]:
    """Handovers an officer has validated (acknowledged with open issues, or closed), with their invoices."""
    return _with_items(db, _rows(db, _filtered(db, statuses=("acknowledged", "closed"), q=q, date_from=date_from, date_to=date_to, driver_id=driver_id), viewer_user_id))


def _get(db: Session, handover_id: int) -> InvoiceHandover:
    handover = db.get(InvoiceHandover, handover_id)
    if handover is None:
        fail(404, "Handover not found.", "not_found")
    return handover


def detail(db: Session, handover_id: int, viewer_user_id: int | None) -> dict:
    handover = _get(db, handover_id)
    row = _with_items(db, _rows(db, [handover], viewer_user_id))[0]
    stops = db.execute(select(TripStop).where(TripStop.trip_id == handover.trip_id).order_by(TripStop.sequence)).scalars().all()
    row["stops"] = [{"so_number": s.so_number, "customer_name": s.customer_name, "outcome": s.outcome, "reason": s.reason, "checkout_at": iso(s.checkout_at)} for s in stops]
    events = db.execute(select(TripEvent).where(TripEvent.batch_id == handover.batch_id).order_by(TripEvent.client_ts, TripEvent.id)).scalars().all()
    users = users_by_id(db, [e.user_id for e in events])
    numbers = {i["so_id"]: i["so_number"] for i in row["items"]} | {s.so_id: s.so_number for s in stops}
    row["events"] = [{"type": e.event_type, "at": iso(e.client_ts), "received_at": iso(e.received_at), "by": users.get(e.user_id) or {"user_id": e.user_id, "name": f"User #{e.user_id}", "role": e.role},
                      "so_number": numbers.get(e.so_id) if e.so_id else None} for e in events]
    return row


def outstanding(db: Session, now: datetime | None = None) -> list[dict]:
    """Every invoice not yet received (pending, missing, discrepancy) across all dates, oldest handover first."""
    now = aware(now or trip_events.now_utc())
    found = db.execute(
        select(InvoiceHandoverItem, InvoiceHandover, Trip)
        .join(InvoiceHandover, InvoiceHandover.id == InvoiceHandoverItem.handover_id)
        .join(Trip, Trip.id == InvoiceHandover.trip_id)
        .where(InvoiceHandoverItem.officer_status != "received")
        .order_by(InvoiceHandover.submitted_at, InvoiceHandoverItem.id)
    ).all()
    out = []
    for item, handover, trip in found:
        out.append({
            "item_id": item.id, "handover_id": handover.id, "batch_id": handover.batch_id, "so_number": item.so_number, "customer_name": item.customer_name, "officer_status": item.officer_status,
            "officer_note": item.officer_note, "crew_note": item.crew_note, "handed_over": item.handed_over, "delivery_date": trip.delivery_date.isoformat() if trip.delivery_date else None,
            "driver": {"id": trip.driver_staff_id, "name": staff_name(trip.driver_staff_id)}, "submitted_at": iso(handover.submitted_at),
            "age_hours": round(max((now - aware(handover.submitted_at)).total_seconds(), 0) / 3600.0, 1),
        })
    return out


# ---- writes ---------------------------------------------------------------------------------------------------------


def _context(body):
    received_at = trip_events.now_utc()
    client_ts, _ = trip_events.parse_client_ts(body.client_ts, received_at)
    return received_at, client_ts, trip_events.clean_gps(body.gps, body.gps_denied_reason)


def _done_by(db: Session, user_id: int | None, at: datetime | None, fallback_role: str = "logistics_officer") -> dict | None:
    if user_id is None:
        return None
    user = users_by_id(db, [user_id]).get(user_id)
    return {"name": user["name"] if user else f"User #{user_id}", "role": user["role"] if user else fallback_role, "at": iso(at)}


def _respond(db: Session, handover_id: int, viewer_user_id: int, *, already_done=False, done_by=None) -> dict:
    return {"ok": True, "already_done": already_done, "replayed": False, "done_by": done_by, "handover": detail(db, handover_id, viewer_user_id)}


def _close(db: Session, handover: InvoiceHandover, trip: Trip | None, actor: Actor, received_at, client_ts, gps) -> None:
    handover.status = "closed"
    if trip is not None and trip.status == "handover_submitted":
        trip.status = "closed"
        trip.closed_at = client_ts
        trip_events.add_event(db, trip_id=trip.id, batch_id=trip.batch_id, event_type="trip_closed", actor=actor, received_at=received_at, client_ts=client_ts, gps=gps)


def validate(db: Session, actor: Actor, handover_id: int, body) -> dict:
    """body: idempotency_key, client_ts, gps, gps_denied_reason, items=[{item_id, status, note}]."""
    replay = trip_events.replay_of(db, body.idempotency_key)
    if replay is not None:
        return replay
    handover = _get(db, handover_id)
    items = list(db.execute(select(InvoiceHandoverItem).where(InvoiceHandoverItem.handover_id == handover.id).order_by(InvoiceHandoverItem.id)).scalars().all())
    by_id = {i.id: i for i in items}

    errors: list[dict] = []
    given: dict[int, tuple[str, str | None]] = {}
    for entry in body.items:
        if entry.item_id not in by_id:
            errors.append({"key": str(entry.item_id), "message": "This invoice is not part of the handover."})
            continue
        if entry.item_id in given:
            errors.append({"key": str(entry.item_id), "message": "Listed twice."})
            continue
        status, note = (entry.status or "").strip().lower(), _text(entry.note)
        if status not in ITEM_STATUSES:
            errors.append({"key": str(entry.item_id), "message": "Choose received, missing or discrepancy."})
        elif status in NOTE_REQUIRED and not note:
            errors.append({"key": str(entry.item_id), "message": f"A note is required when an invoice is {status}."})
        given[entry.item_id] = (status, note)
    for item in items:
        if item.id not in given:
            errors.append({"key": str(item.id), "message": f"Give {item.so_number or 'this invoice'} a status (received, missing or discrepancy)."})
    if errors:
        fail(422, "Check every invoice before validating.", "validation", errors)

    unchanged = handover.status != "pending" and all((by_id[i].officer_status, by_id[i].officer_note) == given[i] for i in given)
    if unchanged:
        return _respond(db, handover.id, actor.user_id, already_done=True, done_by=_done_by(db, handover.officer_acknowledged_by_user_id, handover.officer_acknowledged_at))
    if handover.status == "closed":
        fail(409, "This handover is already closed.", "out_of_order")

    trip = db.get(Trip, handover.trip_id)
    received_at, client_ts, gps = _context(body)
    first_validation = handover.officer_acknowledged_at is None
    for item in items:
        status, note = given[item.id]
        was_open = item.officer_status in ("missing", "discrepancy")
        item.officer_status, item.officer_note, item.officer_user_id = status, note, actor.user_id
        if item.officer_checked_at is None or status != "received":
            item.officer_checked_at = client_ts
        if status == "received" and was_open:
            item.resolved_at = client_ts  # a previously missing / discrepant invoice is now in hand: keeps officer_checked_at
    if first_validation:
        handover.officer_acknowledged_at, handover.officer_acknowledged_by_user_id = client_ts, actor.user_id
    all_received = all(given[i.id][0] == "received" for i in items)
    if all_received:
        _close(db, handover, trip, actor, received_at, client_ts, gps)
    else:
        handover.status = "acknowledged"
    db.flush()

    result = _respond(db, handover.id, actor.user_id)
    summary = {str(i.id): given[i.id][0] for i in items}
    first = True
    if first_validation:
        trip_events.add_event(db, trip_id=handover.trip_id, batch_id=handover.batch_id, event_type="officer_acknowledged", actor=actor, received_at=received_at, client_ts=client_ts, gps=gps,
                              key=body.idempotency_key, payload={"handover_id": handover.id, "items": summary}, result=result)
        first = False
    for item in items:
        trip_events.add_event(db, trip_id=handover.trip_id, batch_id=handover.batch_id, event_type="invoice_validated", actor=actor, received_at=received_at, client_ts=client_ts, gps=gps, so_id=item.so_id,
                              key=body.idempotency_key if first else None, payload={"item_id": item.id, "status": given[item.id][0], "note": given[item.id][1]}, result=result if first else None)
        first = False
    replay = trip_events.commit_or_replay(db, body.idempotency_key)
    if replay is not None:
        return replay
    trip_events.bump()
    return result


def resolve(db: Session, actor: Actor, item_id: int, body) -> dict:
    """body: idempotency_key, client_ts, gps, gps_denied_reason, status ("received"), note."""
    replay = trip_events.replay_of(db, body.idempotency_key)
    if replay is not None:
        return replay
    item = db.get(InvoiceHandoverItem, item_id)
    if item is None:
        fail(404, "Invoice not found.", "not_found")
    handover = db.get(InvoiceHandover, item.handover_id)
    if (body.status or "").strip().lower() != "received":
        fail(422, "A later resolution marks the invoice as received.", "validation", [{"key": "status", "message": "Only received is accepted here."}])
    if item.officer_status == "received":
        return _respond(db, handover.id, actor.user_id, already_done=True, done_by=_done_by(db, item.officer_user_id, item.resolved_at or item.officer_checked_at))

    trip = db.get(Trip, handover.trip_id)
    received_at, client_ts, gps = _context(body)
    note = _text(body.note)
    item.officer_status, item.officer_user_id, item.resolved_at = "received", actor.user_id, client_ts
    if note:
        item.officer_note = note
    if item.officer_checked_at is None:
        item.officer_checked_at = client_ts
    if handover.officer_acknowledged_at is None:
        handover.officer_acknowledged_at, handover.officer_acknowledged_by_user_id = client_ts, actor.user_id
    db.flush()
    still_open = db.execute(select(func.count()).select_from(InvoiceHandoverItem).where(InvoiceHandoverItem.handover_id == handover.id, InvoiceHandoverItem.officer_status != "received")).scalar_one()
    closing = still_open == 0
    if closing:
        _close(db, handover, trip, actor, received_at, client_ts, gps)
        db.flush()
    elif handover.status == "pending":
        handover.status = "acknowledged"
    result = _respond(db, handover.id, actor.user_id)
    trip_events.add_event(db, trip_id=handover.trip_id, batch_id=handover.batch_id, event_type="invoice_resolved", actor=actor, received_at=received_at, client_ts=client_ts, gps=gps, so_id=item.so_id,
                          key=body.idempotency_key, payload={"item_id": item.id, "status": "received", "note": note}, result=result)
    replay = trip_events.commit_or_replay(db, body.idempotency_key)
    if replay is not None:
        return replay
    trip_events.bump()
    return result
