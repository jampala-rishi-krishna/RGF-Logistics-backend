from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from auth.dependencies import require_role
from services import email_conversations, gmail_sender, twilio_logs

router = APIRouter(prefix="/api/communications", tags=["communications"], dependencies=[Depends(require_role("admin", "dispatcher", "warehouse"))])


@router.get("/email/conversations")
def email_conversation_feed():
    """Logistics email conversations built from Gmail threads (label Logistics, last 30 days).
    Read-only; cached 60s; same shape the Comms views used to get from the n8n feed."""
    try:
        return email_conversations.feed()
    except gmail_sender.GmailSendError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "The email conversation feed is unavailable.") from exc


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
