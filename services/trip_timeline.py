"""One timeline builder for crew history, officer detail and the Driver Overview. Contract: docs/TRIP_WORKFLOW.md section 7.

Pure reads: Neon (trips, stops, acknowledgements, handovers, events, templates, sales_orders for batches that have no trip
yet) + memory (staff directory cache). No Zoho. Queries are batched per batch (one query per table, never per SO).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.sales_order_history import SalesOrderHistory
from models.trip import FormTemplateItem, InvoiceHandover, InvoiceHandoverItem, SoAcknowledgement, Trip, TripEvent, TripStop
from models.user import User
from services import staff_directory_cache
from services.staff_roles import helper_ids_of

VISIBLE_STATUSES = ("assigned", "manifested", "completed", "delivered")
PHOTO_TYPES = ("photo", "signature")


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def iso(value: datetime | None) -> str | None:
    value = aware(value)
    return value.isoformat() if value else None


def minutes_between(start: datetime | None, end: datetime | None) -> float | None:
    start, end = aware(start), aware(end)
    if start is None or end is None:
        return None
    return round(max((end - start).total_seconds(), 0) / 60.0, 1)


def staff_name(staff_id: int | None) -> str | None:
    if staff_id is None:
        return None
    staff = staff_directory_cache.get_by_id(staff_id) or {}
    return staff.get("name") or f"Staff #{staff_id}"


def chip_for(status: str | None, acknowledged: int, total: int) -> str:
    """Display chip of a batch (same vocabulary as docs section 2)."""
    if status in (None, "pending"):
        return "to_acknowledge" if acknowledged < total or total == 0 else "ready"
    return {"on_trip": "on_trip", "returned": "returned", "handover_submitted": "handover_pending", "closed": "closed"}.get(status, "to_acknowledge")


def batch_rows(db: Session, batch_id: str) -> list[SalesOrderHistory]:
    """Assigned sales-order rows of a batch in assignment order. Legacy (pre batch-id) keys have no trip workflow: empty."""
    if not batch_id or batch_id.startswith("legacy-"):
        return []
    rows = db.execute(select(SalesOrderHistory).where(SalesOrderHistory.assignment_batch_id == batch_id, SalesOrderHistory.assignment_status.in_(VISIBLE_STATUSES))).scalars().all()
    return sorted(rows, key=lambda r: (aware(r.assigned_at).timestamp() if r.assigned_at else 0.0, str(r.salesorder_number or r.id)))


def batch_crew(db: Session, batch_id: str) -> dict | None:
    """Who is on a batch: {"driver_id","helper_ids","delivery_date","vehicle"} from its trip snapshot when the trip exists
    (frozen at first action), else from the assigned sales-order rows. None when the batch does not exist."""
    trip = db.execute(select(Trip).where(Trip.batch_id == batch_id)).scalar_one_or_none()
    if trip is not None:
        return {"driver_id": trip.driver_staff_id, "helper_ids": helper_ids_of(trip.driver_staff_id, trip.helper_staff_ids), "delivery_date": trip.delivery_date, "vehicle": trip.vehicle}
    rows = batch_rows(db, batch_id)
    if not rows:
        return None
    first = rows[0]
    return {"driver_id": first.driver_id, "helper_ids": helper_ids_of(first.driver_id, first.helper_ids), "delivery_date": first.expected_shipment_date, "vehicle": first.vehicle_id}


def is_crew(crew: dict, staff_id: int | None) -> bool:
    return staff_id is not None and (staff_id == crew["driver_id"] or staff_id in crew["helper_ids"])


def users_by_id(db: Session, ids) -> dict[int, dict]:
    wanted = {int(i) for i in ids if i is not None}
    if not wanted:
        return {}
    rows = db.execute(select(User.id, User.full_name, User.role).where(User.id.in_(wanted))).all()
    return {r.id: {"user_id": r.id, "name": r.full_name, "role": r.role} for r in rows}


def _by(users: dict[int, dict], user_id: int | None, fallback_role: str | None = None) -> dict | None:
    if user_id is None:
        return None
    found = users.get(int(user_id))
    if found:
        return dict(found)
    return {"user_id": int(user_id), "name": f"User #{user_id}", "role": fallback_role}


def _ack_value(item_type: str | None, answer: dict):
    if item_type == "checkbox":
        return bool(answer.get("checked"))
    if item_type == "count":
        return answer.get("count")
    if item_type == "photo":
        return answer.get("photo_id")
    if answer.get("text") is not None:
        return answer.get("text")
    if answer.get("photo_id") is not None:
        return answer.get("photo_id")
    if answer.get("count") is not None:
        return answer.get("count")
    return answer.get("checked") if answer.get("checked") is not None else answer.get("note")


def _template_items(db: Session, template_ids: set[int]) -> dict[int, list[FormTemplateItem]]:
    grouped: dict[int, list[FormTemplateItem]] = defaultdict(list)
    if template_ids:
        for item in db.execute(select(FormTemplateItem).where(FormTemplateItem.template_id.in_(template_ids)).order_by(FormTemplateItem.position)).scalars().all():
            grouped[item.template_id].append(item)
    return grouped


def _ack_payload(ack: SoAcknowledgement, items: list[FormTemplateItem], users: dict[int, dict]) -> dict:
    by_id = {i.id: i for i in items}
    by_key = {i.key: i for i in items}
    answers = []
    for answer in ack.answers or []:
        item = by_id.get(answer.get("item_id")) or by_key.get(answer.get("key"))
        item_type = item.item_type if item else None
        answers.append({"label": item.label if item else (answer.get("key") or f"Item {answer.get('item_id')}"), "type": item_type, "value": _ack_value(item_type, answer), "note": answer.get("note")})
    return {"at": iso(ack.acknowledged_at), "received_at": iso(ack.received_at), "by": _by(users, ack.by_user_id, ack.by_role), "template_version": ack.template_version, "answers": answers, "discrepancy_note": ack.discrepancy_note, "gps": ack.gps}


def _checkout_answers(stop: TripStop, items: list[FormTemplateItem]) -> list[dict]:
    by_key = {i.key: i for i in items}
    out = []
    for answer in stop.form_answers or []:
        item = by_key.get(answer.get("key"))
        entry = {"label": item.label if item else (answer.get("label") or answer.get("key")), "value": answer.get("value")}
        if item is not None and item.item_type in PHOTO_TYPES and answer.get("value") not in (None, ""):
            entry["photo_url"] = f"/api/media/{answer['value']}"
        out.append(entry)
    return out


def _stop_payload(stop: TripStop, items: list[FormTemplateItem], users: dict[int, dict]) -> dict:
    checkin = None
    if stop.checkin_at is not None:
        checkin = {"at": iso(stop.checkin_at), "received_at": iso(stop.checkin_received_at), "by": _by(users, stop.checkin_by_user_id), "gps": stop.checkin_gps, "distance_m": stop.checkin_distance_m, "far": stop.checkin_far}
    checkout = None
    if stop.checkout_at is not None:
        checkout = {"at": iso(stop.checkout_at), "received_at": iso(stop.checkout_received_at), "by": _by(users, stop.checkout_by_user_id), "gps": stop.checkout_gps, "outcome": stop.outcome, "reason": stop.reason,
                    "answers": _checkout_answers(stop, items), "template_version": stop.form_template_version}
    return {"sequence": stop.sequence, "status": stop.status, "checkin": checkin, "checkout": checkout, "minutes_at_stop": minutes_between(stop.checkin_at, stop.checkout_at)}


def _handover_item_payload(item: InvoiceHandoverItem, users: dict[int, dict]) -> dict:
    return {"item_id": item.id, "handed_over": item.handed_over, "crew_note": item.crew_note, "officer_status": item.officer_status, "officer_note": item.officer_note,
            "checked_at": iso(item.officer_checked_at), "checked_by": _by(users, item.officer_user_id), "resolved_at": iso(item.resolved_at)}


def build_timeline(db: Session, batch_id: str) -> dict | None:
    """Full read-only timeline of a batch, or None when no such batch."""
    trip = db.execute(select(Trip).where(Trip.batch_id == batch_id)).scalar_one_or_none()
    rows = batch_rows(db, batch_id)
    if trip is None and not rows:
        return None

    stops: list[TripStop] = []
    acks: dict[str, SoAcknowledgement] = {}
    handover: InvoiceHandover | None = None
    items_by_so: dict[str, InvoiceHandoverItem] = {}
    events: list[TripEvent] = []
    if trip is not None:
        stops = sorted(db.execute(select(TripStop).where(TripStop.trip_id == trip.id)).scalars().all(), key=lambda s: s.sequence)
        acks = {a.so_id: a for a in db.execute(select(SoAcknowledgement).where(SoAcknowledgement.trip_id == trip.id)).scalars().all()}
        handover = db.execute(select(InvoiceHandover).where(InvoiceHandover.trip_id == trip.id)).scalar_one_or_none()
        if handover is not None:
            items_by_so = {i.so_id: i for i in db.execute(select(InvoiceHandoverItem).where(InvoiceHandoverItem.handover_id == handover.id)).scalars().all()}
    events = sorted(db.execute(select(TripEvent).where(TripEvent.batch_id == batch_id)).scalars().all(), key=lambda e: (aware(e.client_ts), e.id))

    user_ids: set[int] = set()
    if trip is not None:
        user_ids |= {trip.started_by_user_id, trip.ended_by_user_id}
    for stop in stops:
        user_ids |= {stop.checkin_by_user_id, stop.checkout_by_user_id}
    user_ids |= {a.by_user_id for a in acks.values()} | {e.user_id for e in events}
    if handover is not None:
        user_ids |= {handover.submitted_by_user_id, handover.officer_user_id, handover.officer_acknowledged_by_user_id}
    user_ids |= {i.officer_user_id for i in items_by_so.values()}
    users = users_by_id(db, user_ids)

    template_ids = {a.template_id for a in acks.values()} | {s.form_template_id for s in stops if s.form_template_id}
    template_items = _template_items(db, {t for t in template_ids if t})

    row_by_so = {r.id: r for r in rows}
    first = rows[0] if rows else None
    driver_id = trip.driver_staff_id if trip is not None else first.driver_id
    helper_ids = helper_ids_of(driver_id, trip.helper_staff_ids if trip is not None else first.helper_ids)
    crew = [{"id": driver_id, "name": staff_name(driver_id), "role": "driver", "phone": (staff_directory_cache.get_by_id(driver_id) or {}).get("phone") if driver_id is not None else None}]
    crew += [{"id": h, "name": staff_name(h), "role": "helper", "phone": (staff_directory_cache.get_by_id(h) or {}).get("phone")} for h in helper_ids]
    delivery_date = trip.delivery_date if trip is not None else first.expected_shipment_date
    vehicle = trip.vehicle if trip is not None else first.vehicle_id

    order_ids = [s.so_id for s in stops] or [r.id for r in rows]
    for so_id in list(acks) + list(items_by_so):  # SOs known only from workflow rows (e.g. reassigned after the trip began)
        if so_id not in order_ids:
            order_ids.append(so_id)
    stop_by_so = {s.so_id: s for s in stops}
    orders = []
    for so_id in order_ids:
        row, stop, ack, item = row_by_so.get(so_id), stop_by_so.get(so_id), acks.get(so_id), items_by_so.get(so_id)
        orders.append({
            "so_id": so_id,
            "so_number": (stop.so_number if stop else None) or (row.salesorder_number if row else None) or (item.so_number if item else None) or so_id,
            "customer_name": (stop.customer_name if stop else None) or (row.customer_name if row else None) or (item.customer_name if item else None),
            "address_text": stop.address_text if stop else None,
            "ack": _ack_payload(ack, template_items.get(ack.template_id, []), users) if ack else None,
            "stop": _stop_payload(stop, template_items.get(stop.form_template_id, []) if stop.form_template_id else [], users) if stop else None,
            "handover": _handover_item_payload(item, users) if item else None,
        })
    number_by_so = {o["so_id"]: o["so_number"] for o in orders}

    acknowledged = sum(1 for o in orders if o["ack"])
    status = trip.status if trip is not None else "pending"
    done = sum(1 for s in stops if s.status == "checked_out")
    trip_block = {
        "status": status, "chip": chip_for(status, acknowledged, len(orders)), "acknowledged": acknowledged, "total": len(orders), "stops_done": done,
        "started_at": iso(trip.started_at) if trip else None, "started_by": _by(users, trip.started_by_user_id) if trip else None, "started_gps": trip.started_gps if trip else None,
        "ended_at": iso(trip.ended_at) if trip else None, "ended_by": _by(users, trip.ended_by_user_id) if trip else None, "ended_gps": trip.ended_gps if trip else None,
        "end_distance_m": trip.end_distance_m if trip else None, "closed_at": iso(trip.closed_at) if trip else None, "stop_order_source": trip.stop_order_source if trip else None,
        "duration_minutes": minutes_between(trip.started_at, trip.ended_at) if trip else None,
        "handover_status": handover.status if handover else None,
    }
    trip_block["trip_minutes"] = trip_block["duration_minutes"]
    handover_block = None
    if handover is not None:
        handover_block = {"handover_id": handover.id, "status": handover.status, "submitted_at": iso(handover.submitted_at), "submitted_by": _by(users, handover.submitted_by_user_id),
                          "officer": _by(users, handover.officer_user_id), "officer_acknowledged_at": iso(handover.officer_acknowledged_at), "note": handover.note}
    return {
        "batch": {"batch_id": batch_id, "delivery_date": delivery_date.isoformat() if delivery_date else None, "vehicle": vehicle, "so_count": len(orders), "warehouse": trip.warehouse if trip else None},
        "trip": trip_block,
        "crew": crew,
        "orders": orders,
        "handover": handover_block,
        "events": [{"type": e.event_type, "at": iso(e.client_ts), "received_at": iso(e.received_at), "by": _by(users, e.user_id, e.role), "so_number": number_by_so.get(e.so_id) if e.so_id else None, "gps": e.gps} for e in events],
    }
