from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from auth.dependencies import require_role
from services import twilio_logs

router = APIRouter(prefix="/api/communications", tags=["communications"], dependencies=[Depends(require_role("admin", "dispatcher", "warehouse"))])


@router.get("/whatsapp/logs")
def whatsapp_logs(since: str = Query(..., description="YYYY-MM-DD (UTC date sent, inclusive)")):
    try:
        date.fromisoformat(since)
    except ValueError as exc:
        raise HTTPException(422, "since must be YYYY-MM-DD.") from exc
    try:
        return twilio_logs.get_logs(since)
    except twilio_logs.TwilioNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except twilio_logs.TwilioError as exc:
        raise HTTPException(502, str(exc)) from exc
