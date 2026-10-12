from __future__ import annotations

import os
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, require_role
from database import get_db
from services import driver_assignments, staff_directory_cache
from services.staff_roles import staff_kind

# Driver app API (drivers AND helpers). Reads Neon (history rows written at assignment, trip workflow rows) and memory only -
# no Zoho call anywhere in this module. Every handler resolves ONE staff id first (resolve_driver_id) and passes it to every query.
router = APIRouter(prefix="/api/driver", tags=["driver"], dependencies=[Depends(require_role("driver", "helper", "admin"))])

CREW_ROLES = ("driver", "helper")


def resolve_driver_id(user: CurrentUser, driver_id: int | None) -> int:
    """Whose data this request may read.

    - driver / helper: always their own linked staff id. A `driver_id` that names anyone else is refused, so changing the
      URL parameter can never expose another person.
    - admin: must name the staff member with ?driver_id=.
    """
    if user.role in CREW_ROLES:
        if user.staff_id is None:
            raise HTTPException(403, "This login is not linked to a driver record. Contact dispatch.")
        if driver_id is not None and driver_id != user.staff_id:
            raise HTTPException(403, "Forbidden")
        return user.staff_id
    if driver_id is None:
        raise HTTPException(400, "driver_id is required.")
    return driver_id


def can_act(user: CurrentUser) -> bool:
    """Only the crew performs trip actions; an admin reads (so the app shows no action buttons)."""
    return user.role in CREW_ROLES


def _dispatch_phone() -> str | None:
    from services import app_settings

    return app_settings.get_str("DISPATCH_PHONE").strip() or None


@router.get("/me")
def me(driver_id: int | None = Query(None), user: CurrentUser = Depends(require_role("driver", "helper", "admin"))):
    who = resolve_driver_id(user, driver_id)
    staff = staff_directory_cache.get_by_id(who) or {}
    now = driver_assignments.manila_now()
    own = user.role in CREW_ROLES
    return {
        "driver_id": who,
        "role": user.role if own else staff_kind(staff),
        "name": staff.get("name") or (user.full_name if own else f"Staff #{who}"),
        "title": staff.get("title"),
        "warehouse": staff.get("warehouse"),
        "phone": staff.get("phone") or None,
        "dispatch_phone": _dispatch_phone(),
        "now": now.isoformat(),
        "today": now.date().isoformat(),
    }


@router.get("/today")
def today(driver_id: int | None = Query(None), user: CurrentUser = Depends(require_role("driver", "helper", "admin")), db: Session = Depends(get_db)):
    who = resolve_driver_id(user, driver_id)
    staff = staff_directory_cache.get_by_id(who) or {}
    return {
        "date": driver_assignments.manila_today().isoformat(),
        "driver": {"id": who, "name": staff.get("name")},
        "batches": driver_assignments.today_batches(db, who, can_act(user)),
    }


def _parse_day(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"{label} must be YYYY-MM-DD.") from None


@router.get("/history")
def history(
    driver_id: int | None = Query(None),
    days: int | None = Query(None, ge=1, le=driver_assignments.MAX_RANGE_DAYS),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    user: CurrentUser = Depends(require_role("driver", "helper", "admin")),
    db: Session = Depends(get_db),
):
    """Past assignments (dates before today, Manila), newest first. Either ?days=7|30 or an explicit ?from=&to=."""
    who = resolve_driver_id(user, driver_id)
    yesterday = driver_assignments.manila_today() - timedelta(days=1)
    if from_ or to:
        if not (from_ and to):
            raise HTTPException(400, "Both from and to are required.")
        start, end = _parse_day(from_, "from"), _parse_day(to, "to")
        if start > end:
            raise HTTPException(400, "from must not be after to.")
        if (end - start).days + 1 > driver_assignments.MAX_RANGE_DAYS:
            raise HTTPException(400, f"The date range can be at most {driver_assignments.MAX_RANGE_DAYS} days.")
    else:
        end = yesterday
        start = end - timedelta(days=(days or 7) - 1)
    end = min(end, yesterday)  # today's work lives on the Today page
    if start > end:
        return {"from": start.isoformat(), "to": end.isoformat(), "batches": []}
    return {"from": start.isoformat(), "to": end.isoformat(), "batches": driver_assignments.history_batches(db, who, start, end, can_act(user))}


@router.get("/assignments/{batch_id}")
def assignment_detail(batch_id: str, driver_id: int | None = Query(None), user: CurrentUser = Depends(require_role("driver", "helper", "admin")), db: Session = Depends(get_db)):
    who = resolve_driver_id(user, driver_id)
    detail = driver_assignments.batch_detail(db, who, batch_id, can_act(user))
    if detail is None:
        # Same answer for "does not exist" and "belongs to someone else".
        raise HTTPException(404, "Assignment not found.")
    return detail
