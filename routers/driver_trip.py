"""Crew actions: acknowledge, start, check-in, check-out, end, handover (+ templates, officers, route, timeline).
Contract: docs/TRIP_WORKFLOW.md section 4. Implemented in services/trip_workflow.py. No Zoho call."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, require_role
from database import get_db
from routers.driver import CREW_ROLES, resolve_driver_id
from services import driver_assignments, trip_workflow
from services.trip_events import fail
from services.trip_workflow import AcknowledgeAllBody, AcknowledgeBody, ActionBody, CheckoutBody, HandoverBody

router = APIRouter(prefix="/api/driver", tags=["driver-trip"])

# Actions: drivers and helpers act; an admin passes the guard and is refused (403 forbidden) inside the service, officers and
# dispatchers are refused by the guard (403).
crew_or_admin = require_role("driver", "helper", "admin")


@router.get("/templates")
def templates(user: CurrentUser = Depends(crew_or_admin), db: Session = Depends(get_db)):
    return trip_workflow.templates(db)


@router.get("/officers")
def officers(user: CurrentUser = Depends(crew_or_admin), db: Session = Depends(get_db)):
    return trip_workflow.officers(db)


@router.get("/batches/{batch_id}/route")
def route(batch_id: str, driver_id: int | None = Query(None), user: CurrentUser = Depends(crew_or_admin), db: Session = Depends(get_db)):
    return trip_workflow.route(db, user, batch_id, resolve_driver_id(user, driver_id))


@router.post("/batches/{batch_id}/orders/{so_id}/acknowledge")
def acknowledge(batch_id: str, so_id: str, body: AcknowledgeBody, user: CurrentUser = Depends(crew_or_admin), db: Session = Depends(get_db)):
    return trip_workflow.acknowledge(db, user, batch_id, so_id, body)


@router.post("/batches/{batch_id}/acknowledge-all")
def acknowledge_all(batch_id: str, body: AcknowledgeAllBody, user: CurrentUser = Depends(crew_or_admin), db: Session = Depends(get_db)):
    """One tap acknowledges every order of the trip (contract 1)."""
    return trip_workflow.acknowledge_all(db, user, batch_id, body)


@router.post("/batches/{batch_id}/start")
def start(batch_id: str, body: ActionBody, user: CurrentUser = Depends(crew_or_admin), db: Session = Depends(get_db)):
    return trip_workflow.start(db, user, batch_id, body)


@router.post("/batches/{batch_id}/orders/{so_id}/check-in")
def check_in(batch_id: str, so_id: str, body: ActionBody, user: CurrentUser = Depends(crew_or_admin), db: Session = Depends(get_db)):
    return trip_workflow.check_in(db, user, batch_id, so_id, body)


@router.post("/batches/{batch_id}/orders/{so_id}/check-out")
def check_out(batch_id: str, so_id: str, body: CheckoutBody, user: CurrentUser = Depends(crew_or_admin), db: Session = Depends(get_db)):
    return trip_workflow.check_out(db, user, batch_id, so_id, body)


@router.post("/batches/{batch_id}/end")
def end(batch_id: str, body: ActionBody, user: CurrentUser = Depends(crew_or_admin), db: Session = Depends(get_db)):
    return trip_workflow.end(db, user, batch_id, body)


@router.post("/batches/{batch_id}/handover")
def handover(batch_id: str, body: HandoverBody, user: CurrentUser = Depends(crew_or_admin), db: Session = Depends(get_db)):
    return trip_workflow.handover(db, user, batch_id, body)


@router.get("/batches/{batch_id}/timeline")
def timeline(batch_id: str, user: CurrentUser = Depends(require_role("driver", "helper", "admin", "dispatcher")), db: Session = Depends(get_db)):
    """Read-only full timeline: crew of the batch, or admin / dispatcher."""
    if user.role in CREW_ROLES:
        if user.staff_id is None or not driver_assignments.rows_for_batch(db, user.staff_id, batch_id):
            fail(404, "Assignment not found.", "not_found")
    from services import trip_timeline  # owned by another module; imported lazily

    result = trip_timeline.build_timeline(db, batch_id)
    if result is None:
        fail(404, "Assignment not found.", "not_found")
    return result
