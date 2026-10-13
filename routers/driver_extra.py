"""Driver app: issues, alerts. Crew (driver / helper) only; the data is always the caller's own (user id / staff id from the token).
No Zoho call. Contract: docs/TRIP_WORKFLOW.md section 11."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, require_role
from database import get_db
from services import driver_alerts, driver_issues

router = APIRouter(prefix="/api/driver", tags=["driver-extra"])

crew_only = require_role("driver", "helper")


class MarkReadBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    keys: list[str] | None = None
    all: bool = False


@router.get("/issues/meta")
def issues_meta(user: CurrentUser = Depends(crew_only)):
    return driver_issues.meta()


@router.get("/issues")
def my_issues(user: CurrentUser = Depends(crew_only), db: Session = Depends(get_db)):
    return {"issues": driver_issues.list_own(db, user), **driver_issues.meta()}


@router.post("/issues")
def report_issue(body: driver_issues.IssueBody, user: CurrentUser = Depends(crew_only), db: Session = Depends(get_db)):
    return driver_issues.create(db, user, body)


@router.get("/alerts")
def alerts(user: CurrentUser = Depends(crew_only), db: Session = Depends(get_db)):
    return driver_alerts.listing(db, user)


@router.post("/alerts/read")
def alerts_read(body: MarkReadBody, user: CurrentUser = Depends(crew_only), db: Session = Depends(get_db)):
    return driver_alerts.mark_read(db, user, body.keys, body.all)
