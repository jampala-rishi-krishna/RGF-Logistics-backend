from __future__ import annotations

import os
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, require_role
from database import get_db
from services import driver_assignments, driver_issues, staff_directory_cache
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


def viewer_role(user: CurrentUser, staff: dict) -> str:
    """CONTRACT: "driver" | "helper" - the login's crew role; an admin viewing someone gets that staff record's kind."""
    return user.role if user.role in CREW_ROLES else staff_kind(staff)


def _dispatch_phone() -> str | None:
    from services import app_settings

    return app_settings.get_str("DISPATCH_PHONE").strip() or None


@router.get("/me")
def me(driver_id: int | None = Query(None), user: CurrentUser = Depends(require_role("driver", "helper", "admin")), db: Session = Depends(get_db)):
    who = resolve_driver_id(user, driver_id)
    staff = staff_directory_cache.get_by_id(who) or {}
    now = driver_assignments.manila_now()
    own = user.role in CREW_ROLES
    batches = [b for b in driver_assignments.today_batches(db, who, can_act(user)) if b.get("day") != "tomorrow"]  # today's truck / crew only
    crew: dict[int, dict] = {}
    for batch in batches:
        for person in batch.get("crew_members") or []:
            if person["id"] != who:
                crew.setdefault(person["id"], person)
    return {
        "staff_id": who,
        "email": user.email if own else None,
        "vehicles_today": list(dict.fromkeys(b["vehicle"] for b in batches if b["vehicle"])),
        "crew_today": list(crew.values()),
        "driver_id": who,
        "role": user.role if own else staff_kind(staff),
        "viewer_role": viewer_role(user, staff),
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
    view = driver_assignments.today_view(db, who, can_act(user))
    batches = view["batches"]
    return {
        "date": driver_assignments.manila_today().isoformat(),
        "driver": {"id": who, "name": staff.get("name")},
        "viewer_role": viewer_role(user, staff),
        # KPI tiles count today's work only; tomorrow's batches (day == "tomorrow") are listed for acknowledgement.
        "summary": driver_assignments.day_summary([b for b in batches if b.get("day") != "tomorrow"], driver_issues.count_open(db, user_id=user.id) if user.role in CREW_ROLES else 0),
        "tomorrow": (driver_assignments.manila_today() + timedelta(days=1)).isoformat(),
        "batches": batches,
        "dispatch_changes": view["dispatch_changes"],
        **_flags(db),
    }


def _flags(db: Session) -> dict:
    """Contract 3 flags (also in GET /api/driver/templates). Memory-cached; no extra query per poll."""
    from services import form_templates, media_storage

    return {"acknowledge_checklist_enabled": form_templates.acknowledge_checklist_enabled(db), "photo_storage_enabled": bool(media_storage.is_enabled())}


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
    status: str | None = Query(None, pattern="^(all|completed|failed)$"),
    page: int | None = Query(None, ge=1),
    page_size: int | None = Query(None, ge=1, le=driver_assignments.MAX_PAGE_SIZE),
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
    if status is not None or page is not None or page_size is not None:  # paginated + filtered (the Trips screen)
        return driver_assignments.history_page(db, who, start, end, status=status or "all", page=page or 1, page_size=page_size or 10, can_act=can_act(user))
    return {"from": start.isoformat(), "to": end.isoformat(), "batches": driver_assignments.history_batches(db, who, start, end, can_act(user))}


@router.get("/assignments/{batch_id}")
def assignment_detail(batch_id: str, driver_id: int | None = Query(None), user: CurrentUser = Depends(require_role("driver", "helper", "admin")), db: Session = Depends(get_db)):
    who = resolve_driver_id(user, driver_id)
    detail = driver_assignments.batch_detail(db, who, batch_id, can_act(user))
    if detail is None:
        # Same answer for "does not exist" and "belongs to someone else".
        raise HTTPException(404, "Assignment not found.")
    changes = driver_assignments.today_view(db, who, can_act(user))["dispatch_changes"]  # cached: no extra query on a hit
    return {**detail, "dispatch_changes": [c for c in changes if c["batch_id"] == batch_id], **_flags(db)}
