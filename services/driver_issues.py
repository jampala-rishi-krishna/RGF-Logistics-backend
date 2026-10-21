"""Issues reported by drivers / helpers (from a trip, an order or the profile) and answered by dispatch.

One INSERT per report (idempotent by the phone's key), one UPDATE per dispatch response. No Zoho call. Photos are `trip_photos`
rows already uploaded (the bytes live in R2). A report never changes any trip state: it is a structured message to dispatch.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser
from models.trip import ISSUE_CATEGORIES, ISSUE_PRIORITIES, ISSUE_STATUSES, DriverIssue, TripPhoto
from models.user import User
from services import dispatch_changes, driver_assignments, media_storage, trip_events
from services.trip_events import fail
from services.trip_timeline import iso, staff_name

OPEN_STATUSES = ("submitted", "acknowledged", "in_progress")
STATUS_LABELS = {"submitted": "Submitted", "acknowledged": "Acknowledged", "in_progress": "In progress", "resolved": "Resolved", "closed": "Closed"}
MIN_DESCRIPTION = 5
MAX_DESCRIPTION = 2000


class IssueBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    idempotency_key: str = Field(min_length=8, max_length=100)
    category: str
    priority: str = "normal"
    description: str
    batch_id: str | None = None
    so_id: str | None = None
    photo_ids: list[int] = Field(default_factory=list)
    client_ts: str | None = None


class RespondBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str
    response: str | None = None


def public(issue: DriverIssue, *, reporter: bool = True) -> dict:
    return {
        "id": issue.id, "reference": issue.reference, "category": issue.category, "priority": issue.priority, "status": issue.status, "status_label": STATUS_LABELS.get(issue.status, issue.status),
        "description": issue.description, "batch_id": issue.batch_id, "so_id": issue.so_id, "so_number": issue.so_number, "vehicle": issue.vehicle, "photo_ids": list(issue.photo_ids or []),
        "created_at": iso(issue.created_at), "response": issue.response, "responded_at": iso(issue.responded_at),
        **({"reporter": {"user_id": issue.reporter_user_id, "staff_id": issue.reporter_staff_id, "name": staff_name(issue.reporter_staff_id) if issue.reporter_staff_id is not None else None, "role": issue.reporter_role}} if reporter else {}),
    }


def meta() -> dict:
    return {"categories": list(ISSUE_CATEGORIES), "priorities": list(ISSUE_PRIORITIES)}


def create(db: Session, user: CurrentUser, body: IssueBody) -> dict:
    """Crew only. 200 with the stored report on a replay (same idempotency key); 422 on a bad form; 404 for a batch / SO that is not theirs."""
    if user.role not in ("driver", "helper") or user.staff_id is None:
        fail(403, "Only drivers and helpers can report an issue.", "forbidden")
    existing = db.execute(select(DriverIssue).where(DriverIssue.idempotency_key == body.idempotency_key)).scalar_one_or_none()
    if existing is not None:
        if existing.reporter_user_id != user.id:
            fail(409, "This request id was already used.", "conflict")
        return {"ok": True, "replayed": True, "issue": public(existing)}
    errors: list[dict] = []
    if body.category not in ISSUE_CATEGORIES:
        errors.append({"key": "category", "message": "Choose a category."})
    if body.priority not in ISSUE_PRIORITIES:
        errors.append({"key": "priority", "message": "Priority must be normal, high or urgent."})
    description = (body.description or "").strip()
    if len(description) < MIN_DESCRIPTION:
        errors.append({"key": "description", "message": f"Describe the issue (at least {MIN_DESCRIPTION} characters)."})
    if len(description) > MAX_DESCRIPTION:
        errors.append({"key": "description", "message": f"The description can be at most {MAX_DESCRIPTION} characters."})
    batch_id = (body.batch_id or "").strip() or None
    so_id = (body.so_id or "").strip() or None
    rows = driver_assignments.rows_for_batch(db, user.staff_id, batch_id) if batch_id else []
    if batch_id and not rows:
        dispatch_changes.fail_if_trip_removed(db, user.staff_id, batch_id)  # 409 trip_removed (contract 4)
        fail(404, "Assignment not found.", "not_found")  # same answer for "does not exist" and "not your batch"
    row = None
    if so_id:
        row = next((r for r in rows if r.id == so_id), None)
        if row is None and batch_id:
            dispatch_changes.fail_if_order_removed(db, batch_id, so_id)  # 409 order_removed (contract 4)
        if row is None:
            errors.append({"key": "so_id", "message": "That order is not part of this assignment."})
    photo_ids = list(dict.fromkeys(body.photo_ids))[:5]
    if photo_ids:
        found = set(db.execute(select(TripPhoto.id).where(TripPhoto.id.in_(photo_ids), TripPhoto.uploaded_by_user_id == user.id)).scalars().all())
        if found != set(photo_ids):
            errors.append({"key": "photo_ids", "message": "A photo was not found. Upload it again."})
    if errors:
        fail(422, "Please check the issue report.", "validation", errors)
    received = trip_events.now_utc()
    client_ts, _ = trip_events.parse_client_ts(body.client_ts, received)
    issue = DriverIssue(
        reporter_user_id=user.id, reporter_staff_id=user.staff_id, reporter_role=("driver" if rows[0].driver_id == user.staff_id else "helper") if rows else user.role, batch_id=batch_id, so_id=so_id, so_number=(row.salesorder_number or row.id) if row is not None else None,
        vehicle=rows[0].vehicle_id if rows else None, category=body.category, priority=body.priority, description=description, photo_ids=photo_ids or None, status="submitted",
        client_ts=client_ts, idempotency_key=body.idempotency_key,
    )
    db.add(issue)
    try:
        db.flush()
        issue.reference = f"IS-{received.astimezone(driver_assignments.MANILA):%Y%m%d}-{issue.id:04d}"
        db.commit()
    except IntegrityError:  # a concurrent retry with the same key won
        db.rollback()
        winner = db.execute(select(DriverIssue).where(DriverIssue.idempotency_key == body.idempotency_key)).scalar_one_or_none()
        if winner is None:
            raise
        return {"ok": True, "replayed": True, "issue": public(winner)}
    trip_events.bump()  # Driver Overview KPIs / alerts caches
    media_storage.after_issue(db, issue)  # committed: best-effort IS- reference + category on the Drive photos, off the request (never raises)
    return {"ok": True, "replayed": False, "issue": public(issue)}


def list_own(db: Session, user: CurrentUser, limit: int = 50) -> list[dict]:
    if user.staff_id is None:
        return []
    rows = db.execute(select(DriverIssue).where(DriverIssue.reporter_user_id == user.id).order_by(DriverIssue.created_at.desc(), DriverIssue.id.desc()).limit(limit)).scalars().all()
    return [public(r, reporter=False) for r in rows]


def count_open(db: Session, *, user_id: int | None = None) -> int:
    query = select(func.count()).select_from(DriverIssue).where(DriverIssue.status.in_(OPEN_STATUSES))
    if user_id is not None:
        query = query.where(DriverIssue.reporter_user_id == user_id)
    return int(db.execute(query).scalar() or 0)


def list_all(db: Session, status: str | None = None, priority: str | None = None, limit: int = 200) -> list[dict]:
    """Dispatch / admin view: open first, urgent first, newest first."""
    query = select(DriverIssue)
    if status == "open":
        query = query.where(DriverIssue.status.in_(OPEN_STATUSES))
    elif status in ISSUE_STATUSES:
        query = query.where(DriverIssue.status == status)
    elif status not in (None, "", "all"):
        fail(422, f"status must be open, all or one of {', '.join(ISSUE_STATUSES)}.", "validation")
    if priority in ISSUE_PRIORITIES:
        query = query.where(DriverIssue.priority == priority)
    rows = db.execute(query.order_by(DriverIssue.created_at.desc(), DriverIssue.id.desc()).limit(limit)).scalars().all()
    weight = {"urgent": 0, "high": 1, "normal": 2}
    rows.sort(key=lambda r: (0 if r.status in OPEN_STATUSES else 1, weight.get(r.priority, 3)))
    names = {u.id: u.full_name for u in db.execute(select(User).where(User.id.in_({r.responded_by_user_id for r in rows if r.responded_by_user_id}))).scalars().all()} if rows else {}
    out = []
    for r in rows:
        item = public(r)
        item["responded_by"] = names.get(r.responded_by_user_id)
        out.append(item)
    return out


def respond(db: Session, user: CurrentUser, issue_id: int, body: RespondBody) -> dict:
    """Admin / dispatcher: set the status and (optionally) write the reply the driver will see. ONE UPDATE; a no-op writes nothing."""
    issue = db.get(DriverIssue, issue_id)
    if issue is None:
        fail(404, "Issue not found.", "not_found")
    if body.status not in ISSUE_STATUSES:
        fail(422, "Choose a valid status.", "validation", [{"key": "status", "message": f"Status must be one of {', '.join(ISSUE_STATUSES)}."}])
    reply = (body.response or "").strip() or None
    if reply is not None and len(reply) > MAX_DESCRIPTION:
        fail(422, "The reply is too long.", "validation", [{"key": "response", "message": f"At most {MAX_DESCRIPTION} characters."}])
    if issue.status == body.status and (reply is None or reply == issue.response):
        return {"ok": True, "changed": False, "issue": public(issue)}
    issue.status = body.status
    if reply is not None:
        issue.response = reply
    issue.responded_by_user_id = user.id
    issue.responded_at = trip_events.now_utc()
    db.commit()
    trip_events.bump()
    return {"ok": True, "changed": True, "issue": public(issue)}

