"""Automatic unassigns (never a dispatcher click), through the existing unassign pieces of routers/assignment.py:

  * A3 trip end: every SO / WM stop of the ended trip checked out "Not delivered" (failed / returned) or "Cancelled by request" is
    unassigned right after the End-trip commit, so the order returns to the Inventory tab. Trip evidence (stops, photos, events,
    handover items) is kept. The crew gets no "removed by dispatch" banner for these (the trip is over).
  * A4 Zoho date change: on the existing Zoho SO syncs (list windows, delta refresh, detail publish, webhook; NO extra Zoho call),
    an ASSIGNED SO whose Zoho shipment date differs from the date it is assigned for, and that Zoho does not show delivered /
    partially delivered, is unassigned in the background and moved to the new date in memory + history, so it leaves Confirmed
    Orders of the old date and appears in Inventory under the new one. If the crew already acknowledged / started, the unassign
    still happens (the crew app shows its "Dispatch removed SO ... from your trip" banner) and a manager alert is raised.

The manual unassign guard (Zoho fulfilled / shipped, stop checked out) is bypassed (`automatic=True`), EXCEPT that a stop checked out
as Delivered or Partially delivered is never auto-unassigned; Zoho delivered / completed and manifested orders stay blocked.

Audit: the existing `order_unassigned` trip_events row (same rendering in the Driver Overview timeline), written by the system
(user_id 0, role "system") with result.automatic = true, result.reason and result.message ("Auto-unassigned: ...").
Manager alerts are process memory only (shown in the existing /api/access/alerts bell), no table.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import date, datetime, timezone
from itertools import count
from types import SimpleNamespace

from sqlalchemy import select

logger = logging.getLogger(__name__)

SYSTEM_USER = SimpleNamespace(id=0, role="system", staff_id=None, full_name="System (automatic)", email="system", is_manager=False)
NOT_DELIVERED = "not_delivered"
CANCELLED = "cancelled_by_request"
DATE_CHANGED = "date_changed"
CREW_STARTED_STATES = ("acknowledged", "trip_started", "checked_in")
NEVER_STATES = frozenset({"delivered", "partial"})  # a stop checked out with these outcomes is never auto-unassigned


def _spawn(fn) -> None:
    """Run off the sync thread (tests replace this)."""
    threading.Thread(target=fn, daemon=True, name="auto-unassign").start()


def _open_session():
    from database import SessionLocal

    return SessionLocal()


def _bucket(outcome: str | None) -> str | None:
    """delivered | partial | failed | cancelled | None for a stop outcome."""
    text = str(outcome or "").strip().lower()
    if text.replace(" ", "_").startswith(("cancelled_by_request", "canceled_by_request")) or text.startswith(("cancelled", "canceled")):
        return "cancelled"
    from services import media_storage

    kind = media_storage.outcome_kind(outcome)
    return kind if kind in ("delivered", "partial", "failed") else None


def _order_kind(order) -> str:
    from services import transfer_orders

    return transfer_orders.order_type_of(order)


# ---- the unassign itself ----------------------------------------------------------------------------------------------------


def unassign(db, order_id: str, *, reason: str, message: str, notify_crew: bool, new_date: date | None = None, previous_state: str | None = None) -> dict | None:
    """Unassign ONE order automatically and commit. Returns the preview row (state before) or None when skipped. Never calls Zoho."""
    from models.trip import TripStop
    from routers import assignment
    from services import assignment_email_status, dispatch_changes, live_sales_order_cache

    order = live_sales_order_cache.find_cached(order_id)
    if order is None:
        logger.info("[AUTO_UNASSIGN] %s skipped: not in memory (no Zoho call is made for an automatic unassign)", order_id)
        return None
    live_sales_order_cache._apply_assignment(order)  # a window row carries the Zoho shape; the assignment lives in memory state
    if _order_kind(order) not in ("SO", "WM"):
        return None
    try:
        candidate = assignment._candidate(order_id, order, require_assigned=True, automatic=True)
    except TypeError:  # older signature without the manual-guard flag
        candidate = assignment._candidate(order_id, order, require_assigned=True)
    row = dispatch_changes.describe(db, [candidate])[0]
    c = row["_candidate"]
    if c["delivered"] or c["blocker"]:
        logger.info("[AUTO_UNASSIGN] %s skipped: %s", order_id, c["blocker"] or "delivered")
        return None
    if row["_trip_id"] is not None:
        stop = db.execute(select(TripStop).where(TripStop.trip_id == row["_trip_id"], TripStop.so_id == order_id)).scalars().first()
        if stop is not None and stop.status == "checked_out" and _bucket(stop.outcome) in NEVER_STATES:
            logger.info("[AUTO_UNASSIGN] %s skipped: checked out as %s", order_id, stop.outcome)
            return None
    row["blocked"], row["reason"] = False, None  # automatic: a not-delivered / cancelled check-out does not block (manual guard bypassed)
    batch = row["_batch_id"]
    if new_date is not None:
        order.expected_shipment_date = new_date  # history row + Inventory show the new Zoho date
    assignment._apply_unassign(order, db)
    if new_date is not None:
        live_sales_order_cache.set_assignment(order_id, expected_shipment_date=new_date)
    remaining = len(live_sales_order_cache.ids_for_batch(batch)) if batch else 0
    event = dispatch_changes.add_unassign_event(db, user=SYSTEM_USER, row=row, remaining=remaining)
    if event is not None:
        event.result = {**(event.result or {}), "automatic": True, "reason": reason, "message": message, "by_name": "System (automatic)",
                        **({"previous_state": previous_state} if previous_state else {})}
        event.payload = {**(event.payload or {}), "automatic": reason, "notify_crew": notify_crew,
                         **({"new_delivery_date": new_date.isoformat()} if new_date else {})}
    db.commit()
    assignment._after_unassign()
    try:
        assignment_email_status.clear([order_id])
        left = live_sales_order_cache.ids_for_batch(batch) if batch else []
        if left:
            assignment_email_status.record(left, "not_sent")
    except Exception as exc:  # email status is memory bookkeeping; never undo the unassign for it
        logger.warning("[AUTO_UNASSIGN] email status update skipped: %s", type(exc).__name__)
    logger.info("[AUTO_UNASSIGN] %s unassigned (%s): %s", order_id, reason, message)
    return row


# ---- A3: trip end -------------------------------------------------------------------------------------------------------------


def after_trip_end(db, batch_id: str) -> list[str]:
    """Called after End trip COMMITTED. Unassigns the not-delivered / cancelled SO + WM stops of the trip. Never raises."""
    done: list[str] = []
    try:
        from models.trip import Trip, TripStop

        trip = db.execute(select(Trip).where(Trip.batch_id == batch_id)).scalar_one_or_none()
        if trip is None:
            return done
        stops = db.execute(select(TripStop).where(TripStop.trip_id == trip.id, TripStop.status == "checked_out")).scalars().all()
        targets = [(s.so_id, _bucket(s.outcome), (s.reason or "").strip()) for s in stops]
        for so_id, bucket, why in targets:
            if bucket not in ("failed", "cancelled"):
                continue
            label = "not delivered" if bucket == "failed" else "cancelled by request"
            message = f"Auto-unassigned: {label}" + (f" ({why})" if why else "")
            try:
                if unassign(db, so_id, reason=NOT_DELIVERED if bucket == "failed" else CANCELLED, message=message, notify_crew=False,
                            previous_state=NOT_DELIVERED if bucket == "failed" else CANCELLED) is not None:
                    done.append(so_id)
            except Exception as exc:  # one order failing must not stop the others
                db.rollback()
                logger.warning("[AUTO_UNASSIGN] trip end %s / %s failed: %s", batch_id, so_id, type(exc).__name__)
    except Exception as exc:
        logger.warning("[AUTO_UNASSIGN] trip end %s skipped: %s", batch_id, type(exc).__name__)
    return done


# ---- A4: Zoho date change -----------------------------------------------------------------------------------------------------

_pending: dict[str, tuple[date, date]] = {}  # so_id -> (assigned for, new Zoho date)
_pending_lock = threading.Lock()
_running = False


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def observe_zoho_records(records) -> int:
    """Hook on every existing Zoho SO sync (memory only, cheap: one dict lookup per record). Queues the assigned SOs whose Zoho
    shipment date moved. Returns how many were queued. Never raises."""
    queued = 0
    try:
        from services import live_sales_order_cache
        from services.delivery_status import sales_order_delivery_status

        for record in records or ():
            if not isinstance(record, dict):
                continue
            rid = str(record.get("salesorder_id") or record.get("sales_order_id") or "")
            if not rid:
                continue
            state = live_sales_order_cache.get_assignment(rid)
            if not state or state.get("assignment_status") != "assigned":
                continue
            assigned_for = _as_date(state.get("expected_shipment_date"))
            new = _as_date(record.get("shipment_date") or record.get("expected_shipment_date"))
            if assigned_for is None or new is None or new == assigned_for:
                continue
            if sales_order_delivery_status(record) in ("Delivered", "Partially delivered"):
                continue
            with _pending_lock:
                if _pending.get(rid) == (assigned_for, new):
                    continue
                _pending[rid] = (assigned_for, new)
            queued += 1
        if queued:
            _start()
    except Exception as exc:
        logger.warning("[AUTO_UNASSIGN] date-change check skipped: %s", type(exc).__name__)
    return queued


def _start() -> None:
    global _running
    with _pending_lock:
        if _running:
            return
        _running = True
    _spawn(process_date_changes)


def process_date_changes() -> list[str]:
    """Unassign every queued date change (own session). Returns the unassigned ids."""
    global _running
    done: list[str] = []
    try:
        while True:
            with _pending_lock:
                if not _pending:
                    return done
                so_id, (old, new) = next(iter(_pending.items()))
                _pending.pop(so_id, None)
            try:
                with _open_session() as db:
                    message = f"Auto-unassigned: delivery date changed from {old.isoformat()} to {new.isoformat()} by Zoho"
                    row = unassign(db, so_id, reason=DATE_CHANGED, message=message, notify_crew=True, new_date=new)
                    if row is not None:
                        done.append(so_id)
                        if row["state"] in CREW_STARTED_STATES:
                            raise_manager_alert(so_id, row["number"], f"{row['number']}: {message}. Crew progress at that moment: {row['message'].split(': ', 1)[-1]}.", new)
            except Exception as exc:
                logger.warning("[AUTO_UNASSIGN] date change %s failed: %s", so_id, type(exc).__name__)
    finally:
        with _pending_lock:
            _running = False


# ---- manager alerts (memory only; merged into GET /api/access/alerts for managers) -------------------------------------------

_alerts: deque = deque(maxlen=200)
_alert_ids = count(1)
_alert_lock = threading.Lock()


def raise_manager_alert(order_id: str, order_number: str, text: str, delivery_date: date | None = None) -> dict:
    alert = {"id": -next(_alert_ids), "order_id": order_id, "order_number": order_number, "status": "auto_unassigned", "text": text,
             "delivery_date": delivery_date.isoformat() if delivery_date else None, "created_at": datetime.now(timezone.utc).isoformat(), "read_by": set()}
    with _alert_lock:
        _alerts.appendleft(alert)
    logger.warning("[AUTO_UNASSIGN] manager alert: %s", text)
    return alert


def manager_alerts(user_id: int) -> list[dict]:
    with _alert_lock:
        rows = list(_alerts)
    return [{k: v for k, v in a.items() if k != "read_by"} | {"unread": user_id not in a["read_by"]} for a in rows]


def mark_alert_read(alert_id: int, user_id: int) -> bool:
    with _alert_lock:
        for alert in _alerts:
            if alert["id"] == alert_id:
                alert["read_by"].add(user_id)
                return True
    return False


def reset() -> None:
    global _running
    with _pending_lock:
        _pending.clear()
        _running = False
    with _alert_lock:
        _alerts.clear()
