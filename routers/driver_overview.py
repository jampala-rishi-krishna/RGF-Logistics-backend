"""Driver Overview dashboard for admin / dispatcher. Contract: docs/TRIP_WORKFLOW.md section 8.

Read-only over Neon + memory caches (services/overview.py). No Zoho. Template creation is admin only.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, require_role
from database import get_db
from services import driver_issues, overview, reconcile
from services.trip_events import fail
from services.trip_timeline import build_timeline

router = APIRouter(prefix="/api/driver-overview", tags=["driver-overview"], dependencies=[Depends(require_role("admin", "dispatcher"))])

BOARD_STATES = ("idle", "to_acknowledge", "ready", "on_trip", "returned", "handover_pending", "closed")


@router.get("/summary")
def summary(date: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None, warehouse: str | None = None, driver_id: int | None = None, db: Session = Depends(get_db)):
    start, end = overview.parse_range(date, from_, to)
    wh = overview.parse_warehouse(warehouse)
    return overview.cached("summary", (start, end, wh, driver_id), lambda: overview.summary(db, start, end, wh, driver_id))


@router.get("/board")
def board(date: str | None = None, warehouse: str | None = None, status: str | None = None, driver_id: int | None = None, stale_minutes: int | None = Query(None, ge=1, le=1440), db: Session = Depends(get_db)):
    start, _ = overview.parse_range(date, None, None)
    wh = overview.parse_warehouse(warehouse)
    if status and status not in BOARD_STATES:
        fail(422, f"status must be one of {', '.join(BOARD_STATES)}.", "validation")
    stale = stale_minutes or overview.stale_minutes_default()
    return overview.cached("board", (start, wh, status, driver_id, stale), lambda: overview.board(db, start, wh, status, driver_id, stale))


@router.get("/drivers/{staff_id}")
def driver(staff_id: int, from_: str | None = Query(None, alias="from"), to: str | None = None, date: str | None = None, db: Session = Depends(get_db)):
    start, end = overview.parse_range(date, from_, to)
    return overview.cached("driver", (staff_id, start, end), lambda: overview.driver_detail(db, staff_id, start, end))


@router.get("/trips/{batch_id}")
def trip(batch_id: str, db: Session = Depends(get_db)):
    timeline = build_timeline(db, batch_id)
    if timeline is None:
        fail(404, "Trip not found.", "not_found")
    return timeline


@router.get("/map")
def map_view(date: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None, driver_id: int | None = None, warehouse: str | None = None, db: Session = Depends(get_db)):
    start, end = overview.parse_range(date, from_, to)
    wh = overview.parse_warehouse(warehouse)
    return overview.cached("map", (start, end, wh, driver_id), lambda: overview.map_points(db, start, end, wh, driver_id))


@router.get("/export.csv")
def export_csv(date: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None, warehouse: str | None = None, driver_id: int | None = None, db: Session = Depends(get_db)):
    start, end = overview.parse_range(date, from_, to)
    wh = overview.parse_warehouse(warehouse)
    content = overview.cached("csv", (start, end, wh, driver_id), lambda: overview.export_csv(db, start, end, wh, driver_id))
    return Response(content=content, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="driver-overview-{start}-{end}.csv"', "Cache-Control": "private, no-store"})


@router.get("/trips")
def trips(date: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None, warehouse: str | None = None, driver_id: int | None = None, db: Session = Depends(get_db)):
    start, end = overview.parse_range(date, from_, to)
    wh = overview.parse_warehouse(warehouse)
    return overview.cached("trips", (start, end, wh, driver_id), lambda: overview.trips_list(db, start, end, wh, driver_id))


@router.get("/hardcopies")
def hardcopies(warehouse: str | None = None, driver_id: int | None = None, db: Session = Depends(get_db)):
    wh = overview.parse_warehouse(warehouse)
    return overview.cached("hardcopies", (wh, driver_id), lambda: overview.hardcopies(db, wh, driver_id))


@router.get("/issues")
def issues(status: str | None = "open", priority: str | None = None, db: Session = Depends(get_db)):
    """Issues reported from the driver app (open first, urgent first). Read by admin and dispatcher."""
    return {"issues": driver_issues.list_all(db, status, priority), **driver_issues.meta(), "statuses": list(driver_issues.STATUS_LABELS)}


@router.post("/issues/{issue_id}/respond")
def respond_to_issue(issue_id: int, body: driver_issues.RespondBody, user: CurrentUser = Depends(require_role("admin", "dispatcher")), db: Session = Depends(get_db)):
    return driver_issues.respond(db, user, issue_id, body)


@router.get("/reconcile")
def reconcile_day(date: str | None = None, source: str = Query("auto", pattern="^(auto|memory|neon)$"), user: CurrentUser = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin only. For one date: Load Planning Confirmed SO vs the Driver App vs Driver Overview, per person (SOs, kg, packs, batches).
    Never cached, never calls Zoho."""
    day, _ = overview.parse_range(date, None, None)
    return reconcile.reconcile(db, day, source)


# ---- templates (dispatcher reads, admin edits) -------------------------------------------------------------------------


class TemplateItemBody(BaseModel):
    key: str
    label: str
    item_type: str
    required: bool = False
    options: list[str] | None = None
    prefill: str | None = None
    show_when: dict | None = None
    active: bool = True
    app_only: bool = False


class TemplateBody(BaseModel):
    name: str
    items: list[TemplateItemBody] = Field(default_factory=list)


@router.get("/templates")
def templates(db: Session = Depends(get_db)):
    return overview.list_templates(db)


@router.get("/templates/{kind}/versions/{version}")
def template_version(kind: str, version: int, db: Session = Depends(get_db)):
    return overview.get_template_version(db, kind, version)


@router.post("/templates/{kind}", status_code=201)
def create_template(kind: str, body: TemplateBody, user: CurrentUser = Depends(require_role("admin")), db: Session = Depends(get_db)):
    return overview.create_template_version(db, kind, user.id, body.name, [item.model_dump() for item in body.items])
