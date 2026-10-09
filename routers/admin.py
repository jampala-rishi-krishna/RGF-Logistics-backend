from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth.dependencies import require_role
from database import get_db
from models.user import User
from services import memory_tables, zoho_usage
from services.audit import list_audit_log as _list_audit_log

router = APIRouter(tags=["admin"], dependencies=[Depends(require_role("admin"))])

# Same admin auth as the Users page. Lives under /api/admin like the other admin switches.
usage_router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])


@usage_router.get("/zoho-usage")
def get_zoho_usage():
    """Usage tile data. Does at most one Neon read per 5 minutes (refresh_if_stale), only while this is polled."""
    zoho_usage.refresh_if_stale()
    snap = zoho_usage.snapshot()
    return {
        "zoho_calls_today": snap["zoho_calls_today"],
        "process_started_at": snap["process_started_at"],
        "usage_restored_from_db": snap["usage_restored_from_db"],
        "zoho_usage": snap,
    }


VALID_INTEGRATION_STATES = {"not_connected", "sandbox", "live"}

# 2026-09-24 (Step 5/6): audit_log, integration_status, and roles all dropped from Neon
# (not in the final kept-table list) - in-memory/static only now.


@router.get("/audit-log")
def list_audit_log(targetEntity: str | None = None, actorId: str | None = None):
    return _list_audit_log(targetEntity, actorId)


@router.get("/integrations")
def list_integrations():
    return memory_tables.list_integration_status()


class IntegrationStateBody(BaseModel):
    state: str


@router.patch("/integrations/{provider}")
def upsert_integration_state(provider: str, body: IntegrationStateBody):
    if body.state not in VALID_INTEGRATION_STATES:
        raise HTTPException(400, f"state must be one of {', '.join(sorted(VALID_INTEGRATION_STATES))}")
    return memory_tables.upsert_integration_status(provider, body.state)


@router.get("/users")
def list_users(db: Session = Depends(get_db)):
    return [
        {
            "id": u.id,
            "full_name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "role": u.role,
            "status": u.status,
            "profile_photo_url": u.profile_photo_url,
        }
        for u in db.execute(select(User)).scalars().all()
    ]


@router.get("/roles")
def list_roles():
    return memory_tables.ROLES
