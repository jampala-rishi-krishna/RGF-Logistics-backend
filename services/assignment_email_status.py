"""Per-assignment email status (queued / sent / failed / skipped).

One email covers several SOs, so a status is recorded for every sales order of the batch together.
It lives in the live assignment state (memory, shown in the UI immediately) and is persisted to
sales_orders.email_* ONLY on a state change (queued -> sent/failed/skipped), never polled or
re-written. After a restart the status is loaded back with the rest of the assignment state.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import update

from services import live_sales_order_cache

logger = logging.getLogger("assignment_email_status")
STATUSES = ("queued", "sent", "failed", "skipped")
_PRIORITY = ("failed", "queued", "sent", "skipped")


def _open_session():
    from database import SessionLocal
    return SessionLocal()


def record(ids, status: str, *, error: str | None = None, message_id: str | None = None, batch_id: str | None = None) -> None:
    ids = [str(i) for i in dict.fromkeys(ids) if i]
    if not ids or status not in STATUSES:
        return
    fields = {
        "email_status": status,
        "email_error": str(error)[:1000] if error else None,
        "email_sent_at": datetime.now(timezone.utc) if status == "sent" else None,
        "email_message_id": message_id,
    }
    if batch_id:
        fields["assignment_batch_id"] = batch_id  # rides in the same UPDATE as the status: no extra write
    for order_id in ids:
        live_sales_order_cache.set_assignment(order_id, **fields)
    try:
        from models.sales_order_history import SalesOrderHistory
        with _open_session() as db:
            db.execute(update(SalesOrderHistory).where(SalesOrderHistory.id.in_(ids)).values(**fields))
            db.commit()
    except Exception:
        logger.exception("[ASSIGN_EMAIL] could not persist email status %s for %s", status, ids)


def summarize(ids) -> dict:
    """Aggregate the in-memory status of a batch (no database access)."""
    states = [live_sales_order_cache.get_assignment(str(i)) or {} for i in ids]
    statuses = [s.get("email_status") for s in states if s.get("email_status")]
    if not statuses:
        return {"status": None, "error": None, "sentAt": None, "messageId": None}
    status = next(p for p in _PRIORITY if p in statuses)
    pick = next(s for s in states if s.get("email_status") == status)
    sent_at = pick.get("email_sent_at")
    return {
        "status": status,
        "error": pick.get("email_error"),
        "sentAt": sent_at.isoformat() if hasattr(sent_at, "isoformat") else sent_at,
        "messageId": pick.get("email_message_id"),
    }
