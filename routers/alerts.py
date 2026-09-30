from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.dependencies import CurrentUser, require_role
from services import alerts_memory_store

router = APIRouter(prefix="/alerts", tags=["alerts"])

DISPATCHER_OR_ADMIN = require_role("dispatcher", "admin")

# 2026-09-24 (Step 5): alerts/alert_escalations/audit_log are dropped from Neon - nothing
# ever created an Alert row outside of scripts/seed_demo.py, so this is in-memory only now.
# See services/alerts_memory_store.py.


@router.get("")
def list_alerts(status: str | None = None, severity: str | None = None):
    return alerts_memory_store.list_alerts(status, severity)


@router.post("/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: int, current_user: CurrentUser = Depends(DISPATCHER_OR_ADMIN)):
    alert = alerts_memory_store.acknowledge(alert_id, str(current_user.id))
    if alert is None:
        raise HTTPException(404, "Alert not found")
    return alert


class EscalateBody(BaseModel):
    escalatedToUserId: str | None = None
    note: str | None = None


@router.post("/{alert_id}/escalate")
def escalate_alert(alert_id: int, body: EscalateBody, current_user: CurrentUser = Depends(DISPATCHER_OR_ADMIN)):
    alert = alerts_memory_store.escalate(alert_id, str(current_user.id), body.escalatedToUserId or "", body.note or "")
    if alert is None:
        raise HTTPException(404, "Alert not found")
    return alert


class ResolveBody(BaseModel):
    note: str | None = None


@router.post("/{alert_id}/resolve")
def resolve_alert(alert_id: int, body: ResolveBody, current_user: CurrentUser = Depends(DISPATCHER_OR_ADMIN)):
    alert = alerts_memory_store.resolve(alert_id, str(current_user.id), body.note or "")
    if alert is None:
        raise HTTPException(404, "Alert not found")
    return alert
