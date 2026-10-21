"""Dispatch removes work from a crew: the unassign preview / confirm rules, the `order_unassigned` audit event, the driver app's
`dispatch_changes`, and the `order_removed` / `trip_removed` answers to crew actions on removed work.

Approved by Rishi (driver app simplification task): ONE trip_events row per unassigned order (existing table, no new column),
written in the same transaction as the unassign. Reads:
  * preview / unassign: the trips + acknowledgements + stops of the SELECTED orders' batches only (a rare dispatcher action);
  * driver "today": the order_unassigned events of the last few days (inside the driver_assignments in-memory cache, so a
    phone polling every 60 s does not re-read them; the cache is invalidated by the unassign itself);
  * a crew action that does not match its batch / order any more: one indexed lookup, only on that failure path.
No Zoho call anywhere in this module.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.trip import TripEvent
from services import trip_state
from services.trip_events import now_utc

EVENT = "order_unassigned"
MANILA = ZoneInfo("Asia/Manila")
ORDER_REMOVED = "order_removed"
TRIP_REMOVED = "trip_removed"
ORDER_REMOVED_MESSAGE = "This order was removed by dispatch"
TRIP_REMOVED_MESSAGE = "Trip removed by dispatch"
CONFIRM_STATES = ("acknowledged", "trip_started", "checked_in")
LOOKBACK_DAYS = 3  # unassign events read for the driver's "today" (an unassign done yesterday for today's trip still shows)


def hhmm(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=ZoneInfo("UTC"))
    return aware.astimezone(MANILA).strftime("%H:%M")


# ---- preview (contract 6) ------------------------------------------------------------------------------------------------


def candidate(order_id: str, *, number: str | None, order_type: str, batch_id: str | None, blocker: str | None, delivered: bool = False,
              assignment_status: str | None = None, driver_id: int | None = None, helper_ids=None, delivery_date: date | None = None) -> dict:
    """What the router knows about one selected order (from memory, or the Neon row for the preview)."""
    return {"id": order_id, "number": number or order_id, "order_type": order_type, "batch_id": batch_id, "blocker": blocker, "delivered": delivered,
            "assignment_status": assignment_status, "driver_id": driver_id, "helper_ids": list(helper_ids or []), "delivery_date": delivery_date}


def describe(db: Session, candidates: list[dict]) -> list[dict]:
    """One preview row per candidate (contract 6). Internal fields start with "_" (see public())."""
    batch_ids = sorted({c["batch_id"] for c in candidates if c["batch_id"]})
    data = trip_state.load(db, batch_ids, names=True)
    out = []
    for c in candidates:
        trip = data.trips.get(c["batch_id"]) if c["batch_id"] else None
        ack = data.acks.get(trip.id, {}).get(c["id"]) if trip else None
        stop = data.stops.get(trip.id, {}).get(c["id"]) if trip else None
        acknowledged_by = data.user(ack.by_user_id, ack.by_role)["name"] if ack else None
        acknowledged_at = ack.acknowledged_at if ack else None
        started_at = trip.started_at if trip is not None and trip.status != "pending" else None
        checked_in_at = stop.checkin_at if stop is not None and stop.status in ("checked_in", "checked_out") else None
        number = c["number"]
        blocked, reason = False, None
        if c["delivered"] or (stop is not None and stop.status == "checked_out"):
            state, blocked = "delivered", True
            when = hhmm(stop.checkout_at) if stop is not None and stop.checkout_at else None
            reason = c["blocker"] or (f"Already delivered (checked out at {when}). A delivered order cannot be unassigned." if when else "Already delivered. A delivered order cannot be unassigned.")
            message = f"{number}: {reason}"
        else:
            if stop is not None and stop.status == "checked_in":
                state = "checked_in"
                message = f"{number}: trip started {hhmm(started_at) or '-'}, stop checked in {hhmm(checked_in_at) or '-'}"
            elif started_at is not None:
                state = "trip_started"
                message = f"{number}: trip started {hhmm(started_at)}"
            elif ack is not None:
                state = "acknowledged"
                message = f"{number}: acknowledged by {acknowledged_by} at {hhmm(acknowledged_at)}"
            else:
                state = "assigned"
                message = f"{number}: assigned, not acknowledged yet"
            if c["blocker"]:
                blocked, reason = True, c["blocker"]
                message = f"{number}: {reason}"
        out.append({
            "id": c["id"], "number": number, "order_type": c["order_type"], "state": state,
            "acknowledged_by": acknowledged_by, "acknowledged_at": trip_state.iso(acknowledged_at), "trip_started_at": trip_state.iso(started_at), "checked_in_at": trip_state.iso(checked_in_at),
            "needs_confirm": (not blocked) and state in CONFIRM_STATES, "blocked": blocked, "reason": reason, "message": message,
            "_batch_id": c["batch_id"], "_trip_id": trip.id if trip is not None else None, "_candidate": c,
        })
    return out


def public(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def preview_payload(rows: list[dict]) -> dict:
    return {"needs_confirm": any(r["needs_confirm"] for r in rows), "orders": [public(r) for r in rows]}


def confirm_required(rows: list[dict]) -> None:
    """409 confirm_required (contract 7) listing every order that needs the dispatcher's confirmation. Nothing was written."""
    needing = [r for r in rows if r["needs_confirm"]]
    if not needing:
        return
    count = len(needing)
    message = (f"{needing[0]['message']}. Confirm to unassign." if count == 1
               else f"{count} orders were already acknowledged or started by the crew. Confirm to unassign them.")
    raise HTTPException(409, detail={"code": "confirm_required", "message": message, "orders": [public(r) for r in rows]})


# ---- audit event (contract 8) ----------------------------------------------------------------------------------------------


def add_unassign_event(db: Session, *, user, row: dict, remaining: int, now: datetime | None = None) -> TripEvent | None:
    """Adds (does not commit) the ONE order_unassigned row of an unassigned order that belonged to an assignment batch.
    `user` = the dispatcher (CurrentUser). `remaining` = orders still on the batch after this unassign (0 = the trip is gone)."""
    c = row["_candidate"]
    if not row["_batch_id"] or (c["assignment_status"] or "unassigned") == "unassigned":
        return None
    now = now or now_utc()
    event = TripEvent(
        trip_id=row["_trip_id"], batch_id=row["_batch_id"], event_type=EVENT, so_id=row["id"], user_id=user.id, role=user.role or "dispatcher", staff_id=getattr(user, "staff_id", None),
        client_ts=now, received_at=now, gps=None, idempotency_key=None,
        payload={"driver_id": c["driver_id"], "helper_ids": c["helper_ids"], "delivery_date": c["delivery_date"].isoformat() if c["delivery_date"] else None, "remaining_in_batch": remaining},
        result={"so_number": row["number"], "order_type": row["order_type"], "previous_state": row["state"], "acknowledged_by": row["acknowledged_by"], "acknowledged_at": row["acknowledged_at"],
                "trip_started_at": row["trip_started_at"], "checked_in_at": row["checked_in_at"], "by_name": getattr(user, "full_name", None)},
    )
    db.add(event)
    return event


# ---- reading the events back ------------------------------------------------------------------------------------------------


def _crew_ids(event: TripEvent) -> set[int]:
    payload = event.payload or {}
    ids = {payload.get("driver_id")} | set(payload.get("helper_ids") or [])
    return {int(i) for i in ids if i is not None}


def timeline_entry(event: TripEvent, actor_name: str | None) -> dict:
    """Contract 9: how an unassign shows in the Driver Overview timeline."""
    result = event.result or {}
    automatic = bool(result.get("automatic"))  # services/auto_unassign: trip end not delivered / cancelled, Zoho date change
    return {"so_id": event.so_id, "so_number": result.get("so_number") or event.so_id, "order_type": result.get("order_type") or "SO", "previous_state": result.get("previous_state"),
            "actor_name": (result.get("by_name") if automatic else None) or actor_name or result.get("by_name"),
            "automatic": automatic, "reason": result.get("reason"), "message": result.get("message")}


def driver_changes(db: Session, staff_id: int, today: date, current_batch_ids: set[str], also: tuple[date, ...] = ()) -> list[dict]:
    """Contract 3 `dispatch_changes`: today's unassigns on this crew member's batches. A batch that has no order left for them
    is ONE trip_removed entry; otherwise one order_removed entry per removed order. Newest first."""
    since = now_utc() - timedelta(days=LOOKBACK_DAYS)
    events = db.execute(select(TripEvent).where(TripEvent.event_type == EVENT, TripEvent.received_at >= since).order_by(TripEvent.received_at, TripEvent.id)).scalars().all()
    days = {today.isoformat(), *(d.isoformat() for d in also)}  # the driver app lists today + tomorrow (same query, wider filter)
    mine = [e for e in events if staff_id in _crew_ids(e) and (e.payload or {}).get("delivery_date") in days
            and (e.payload or {}).get("notify_crew", True)]  # an automatic unassign after the trip ended (not delivered) is not a dispatch change
    out: list[dict] = []
    gone: dict[str, TripEvent] = {}
    for event in mine:
        if event.batch_id not in current_batch_ids:
            gone[event.batch_id] = event  # latest wins
            continue
        out.append({"kind": ORDER_REMOVED, "batch_id": event.batch_id, "so_id": event.so_id, "so_number": (event.result or {}).get("so_number") or event.so_id, "at": trip_state.iso(event.received_at)})
    for batch_id, event in gone.items():
        out.append({"kind": TRIP_REMOVED, "batch_id": batch_id, "so_id": None, "so_number": None, "at": trip_state.iso(event.received_at)})
    return sorted(out, key=lambda c: c["at"] or "", reverse=True)


# ---- crew actions on removed work (contract 4) --------------------------------------------------------------------------------


def _removed_events(db: Session, batch_id: str, so_id: str | None = None) -> list[TripEvent]:
    query = select(TripEvent).where(TripEvent.batch_id == batch_id, TripEvent.event_type == EVENT)
    if so_id is not None:
        query = query.where(TripEvent.so_id == so_id)
    return list(db.execute(query).scalars().all())


def fail_if_trip_removed(db: Session, staff_id: int | None, batch_id: str | None) -> None:
    """The caller is no longer on `batch_id`: 409 trip_removed when dispatch removed it from them (they WERE crew on it);
    otherwise return so the caller answers its usual 404 (same answer for "does not exist" and "not yours")."""
    if staff_id is None or not batch_id:
        return
    if any(staff_id in _crew_ids(e) for e in _removed_events(db, batch_id)):
        raise HTTPException(409, detail={"code": TRIP_REMOVED, "message": TRIP_REMOVED_MESSAGE})


def fail_if_order_removed(db: Session, batch_id: str, so_id: str | None) -> None:
    """`so_id` is not on the caller's batch any more: 409 order_removed when dispatch unassigned it from this batch."""
    if so_id and _removed_events(db, batch_id, so_id):
        raise HTTPException(409, detail={"code": ORDER_REMOVED, "message": ORDER_REMOVED_MESSAGE})
