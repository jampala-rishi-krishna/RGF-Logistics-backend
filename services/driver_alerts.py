"""Driver alerts: REAL events only, computed from data that already exists, never stored.

  assigned   a new assignment (one per batch, from assigned_at)
  changed    the assignment rows no longer match what the crew started from (vehicle, crew or SO list changed after the first crew action)
  validated  the logistics officer checked an invoice of the crew's trips (received / missing / discrepancy)
  issue      dispatch replied to an issue the user reported

"Sync failed" is a phone-side event (the offline queue) and is shown by the app itself, never invented here.
The only write is one `driver_alert_reads` row per alert marked read (idempotent). No Zoho call.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser
from models.trip import DriverAlertRead, DriverIssue
from services import day_model, driver_assignments
from services.trip_events import fail
from services.trip_timeline import iso
from services.weight_display import format_kg

LOOKBACK_DAYS = 7
LOOKAHEAD_DAYS = 14
DOT = "·"
VALIDATED_TITLES = {"received": "Invoice received", "missing": "Invoice missing", "discrepancy": "Invoice discrepancy"}


def _assignment_link(batch_id: str) -> str:
    return f"/driver/assignment/{batch_id}"


def _kg(facts) -> str:
    text, _note = format_kg(facts.kg, facts.kg_unknown_lines, decimals=1)
    return text


def derive(db: Session, user: CurrentUser) -> list[dict]:
    if user.staff_id is None:
        return []
    today = driver_assignments.manila_today()
    batches = day_model.load(db, today - timedelta(days=LOOKBACK_DAYS), today + timedelta(days=LOOKAHEAD_DAYS), staff_id=user.staff_id)
    cutoff = driver_assignments.manila_now() - timedelta(days=LOOKBACK_DAYS)
    alerts: list[dict] = []
    for facts in batches.values():
        if facts.assigned_at is not None and facts.assigned_at >= cutoff:
            alerts.append({"key": f"assigned:{facts.batch_id}", "type": "assigned", "title": "New assignment", "at": iso(facts.assigned_at), "link": _assignment_link(facts.batch_id),
                           "body": f"{facts.vehicle or 'Truck'} {DOT} {facts.so_count} order{'s' if facts.so_count != 1 else ''} {DOT} {_kg(facts)}", "priority": "normal"})
        trip = facts.trip
        if trip is not None and facts.current_so_ids:
            started_so_ids = set(facts.stops) if facts.stops else None
            crew_then = tuple(sorted({trip.driver_staff_id, *day_model.helper_ids_of(trip.driver_staff_id, trip.helper_staff_ids)} - {None}))
            changed = (
                (trip.vehicle or None) != (facts.current_vehicle or None) or crew_then != facts.current_crew_ids
                or (started_so_ids is not None and started_so_ids != set(facts.current_so_ids))
            )
            if changed:
                digest = hashlib.sha1(f"{facts.current_vehicle}|{facts.current_crew_ids}|{facts.current_so_ids}".encode("utf-8")).hexdigest()[:8]
                alerts.append({"key": f"changed:{facts.batch_id}:{digest}", "type": "changed", "title": "Assignment changed", "at": iso(facts.current_updated_at or facts.assigned_at),
                               "link": _assignment_link(facts.batch_id), "body": f"{facts.current_vehicle or 'Truck'} {DOT} {facts.so_count} orders. Check the vehicle, crew and orders.", "priority": "high"})
        for item in facts.items:
            if item.officer_status in VALIDATED_TITLES and item.officer_checked_at is not None:
                note = f" {DOT} {item.officer_note}" if item.officer_note else ""
                invoice = f" {DOT} invoice {item.invoice_no}" if item.invoice_no else ""
                alerts.append({"key": f"validated:{item.id}:{item.officer_status}", "type": "validated", "title": VALIDATED_TITLES[item.officer_status], "at": iso(item.officer_checked_at),
                               "link": _assignment_link(facts.batch_id), "body": f"{item.so_number or item.so_id}{invoice}{note}", "priority": "normal" if item.officer_status == "received" else "high"})
    for issue in db.execute(select(DriverIssue).where(DriverIssue.reporter_user_id == user.id, DriverIssue.responded_at.is_not(None))).scalars().all():
        stamp = int(issue.responded_at.timestamp())
        alerts.append({"key": f"issue:{issue.id}:{issue.status}:{stamp}", "type": "issue", "title": f"Dispatch replied to {issue.reference}", "at": iso(issue.responded_at), "link": "/driver/issues",
                       "body": issue.response or driver_issue_status(issue.status), "priority": "normal"})
    alerts.sort(key=lambda a: (a["at"] or "", a["key"]), reverse=True)
    return alerts


def driver_issue_status(status: str) -> str:
    return {"submitted": "Submitted", "acknowledged": "Acknowledged", "in_progress": "In progress", "resolved": "Resolved", "closed": "Closed"}.get(status, status)


def listing(db: Session, user: CurrentUser, limit: int = 100) -> dict:
    alerts = derive(db, user)[:limit]
    read = set(db.execute(select(DriverAlertRead.alert_key).where(DriverAlertRead.user_id == user.id)).scalars().all()) if alerts else set()
    for alert in alerts:
        alert["read"] = alert["key"] in read
    return {"alerts": alerts, "unread": sum(1 for a in alerts if not a["read"])}


def mark_read(db: Session, user: CurrentUser, keys: list[str] | None, everything: bool) -> dict:
    """Marks the named alerts (or all current ones) read. Only keys of this user's own current alerts count; a repeat writes nothing."""
    if user.staff_id is None:
        fail(403, "This login is not linked to a driver record.", "forbidden")
    if not everything and not keys:
        fail(422, "Name the alerts to mark as read, or send all=true.", "validation")
    current = {a["key"] for a in derive(db, user)}
    wanted = current if everything else (set(keys or []) & current)
    already = set(db.execute(select(DriverAlertRead.alert_key).where(DriverAlertRead.user_id == user.id, DriverAlertRead.alert_key.in_(wanted))).scalars().all()) if wanted else set()
    new = sorted(wanted - already)
    for key in new:
        db.add(DriverAlertRead(user_id=user.id, alert_key=key))
    if new:
        try:
            db.commit()
        except IntegrityError:  # a concurrent "mark read" wrote the same markers: nothing is lost
            db.rollback()
    return {"ok": True, "marked": len(new), **{"unread": listing(db, user)["unread"]}}
