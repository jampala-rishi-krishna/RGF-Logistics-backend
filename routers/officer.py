"""Logistics officer: handover queue, invoice validation, outstanding invoices, history. Contract: docs/TRIP_WORKFLOW.md section 6."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, require_role
from database import get_db
from services import handover as handover_service
from services.trip_events import Actor, fail

# Reads: logistics_officer, admin, dispatcher / planner. Actions: logistics_officer and admin (recorded as role admin).
router = APIRouter(prefix="/api/officer", tags=["officer"], dependencies=[Depends(require_role("logistics_officer", "admin", "dispatcher"))])
ACT = Depends(require_role("logistics_officer", "admin"))


class ActionBody(BaseModel):
    idempotency_key: str | None = None
    client_ts: str | None = None
    gps: dict | None = None
    gps_denied_reason: str | None = None


class ValidateItem(BaseModel):
    item_id: int
    status: str | None = None
    note: str | None = None


class ValidateBody(ActionBody):
    items: list[ValidateItem] = Field(default_factory=list)


class ResolveBody(ActionBody):
    status: str | None = "received"
    note: str | None = None


def _actor(user: CurrentUser) -> Actor:
    return Actor(user_id=user.id, role="admin" if user.role == "admin" else "logistics_officer", staff_id=user.staff_id, name=user.full_name)


def _day(value: str | None, label: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail(422, f"{label} must be YYYY-MM-DD.", "validation")


def _range(from_: str | None, to: str | None) -> tuple[date | None, date | None]:
    start, end = _day(from_, "from"), _day(to, "to")
    if start and end and start > end:
        fail(422, "from must not be after to.", "validation")
    return start, end


@router.get("/handovers")
def handovers(status: str = "pending", q: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None, driver_id: int | None = None,
              user: CurrentUser = Depends(require_role("logistics_officer", "admin", "dispatcher")), db: Session = Depends(get_db)):
    start, end = _range(from_, to)
    return handover_service.list_handovers(db, user.id, status=status, q=q, date_from=start, date_to=end, driver_id=driver_id)


@router.get("/handovers/{handover_id}")
def handover_detail(handover_id: int, user: CurrentUser = Depends(require_role("logistics_officer", "admin", "dispatcher")), db: Session = Depends(get_db)):
    return handover_service.detail(db, handover_id, user.id)


@router.post("/handovers/{handover_id}/validate")
def validate_handover(handover_id: int, body: ValidateBody, user: CurrentUser = ACT, db: Session = Depends(get_db)):
    return handover_service.validate(db, _actor(user), handover_id, body)


@router.post("/invoices/{item_id}/resolve")
def resolve_invoice(item_id: int, body: ResolveBody, user: CurrentUser = ACT, db: Session = Depends(get_db)):
    return handover_service.resolve(db, _actor(user), item_id, body)


@router.get("/outstanding")
def outstanding(db: Session = Depends(get_db)):
    return handover_service.outstanding(db)


@router.get("/history")
def history(q: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None, driver_id: int | None = None,
            user: CurrentUser = Depends(require_role("logistics_officer", "admin", "dispatcher")), db: Session = Depends(get_db)):
    start, end = _range(from_, to)
    return handover_service.history(db, user.id, q=q, date_from=start, date_to=end, driver_id=driver_id)
