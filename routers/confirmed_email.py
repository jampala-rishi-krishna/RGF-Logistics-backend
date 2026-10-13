"""POST /api/load-planning/email/confirmed/preview | send - warehouse email of Confirmed orders.

Preview builds the files only. Nothing is emailed until `send` is called (the Send button), and it
only ever goes to the recipients in the request body - never to drivers or anyone looked up.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.dependencies import require_role
from database import get_db
from services import confirmed_email as service, gmail_sender

router = APIRouter(prefix="/api/load-planning/email/confirmed", tags=["load-planning"], dependencies=[Depends(require_role("admin", "dispatcher", "warehouse"))])


class PreviewRequest(BaseModel):
    warehouse: str | None = None
    order_ids: list[str] = Field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None


class SendRequest(PreviewRequest):
    to: list[str] | str = Field(default_factory=list)
    cc: list[str] | str = Field(default_factory=list)
    subject: str = ""
    html_body: str = ""


def _fail(exc: service.ConfirmedEmailError):
    raise HTTPException(exc.status_code, str(exc)) from exc


@router.post("/preview")
def preview(body: PreviewRequest, db: Session = Depends(get_db)):
    try:
        return service.preview_payload(service.build_package(db, body.warehouse, body.order_ids, body.date_from, body.date_to))
    except service.ConfirmedEmailError as exc:
        _fail(exc)


@router.post("/send")
def send(body: SendRequest, db: Session = Depends(get_db)):
    """Plain `def`: the order lookup, file building and Gmail call run in a worker thread."""
    try:
        warehouse = service.normalize_warehouse(body.warehouse)
        to = service.normalize_recipients(body.to, "to", required=True)
        cc = [address for address in service.normalize_recipients(body.cc, "cc") if address.lower() not in {item.lower() for item in to}]
        if not body.subject.strip():
            raise service.ConfirmedEmailError("Subject is required.")
    except service.ConfirmedEmailError as exc:
        _fail(exc)
    if not gmail_sender.configured():
        raise HTTPException(503, "Gmail is not configured on the server (GMAIL_COMMS_* credentials).")
    try:
        package = service.build_package(db, warehouse, body.order_ids, body.date_from, body.date_to)
    except service.ConfirmedEmailError as exc:
        _fail(exc)
    try:
        sent = gmail_sender.send_email(
            to=to,
            cc=cc or None,
            subject=body.subject.strip(),
            html=body.html_body or "<p>Confirmed orders attached.</p>",
            attachments=[(package["pdf_filename"], "application/pdf", package["pdf"]), (package["xlsx_filename"], service.XLSX_MIME, package["xlsx"])],
            purpose="confirmed-orders-warehouse-email",
        )
    except gmail_sender.GmailSendError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    return {"success": True, "status": "sent", "messageId": sent["id"], "orderCount": package["order_count"], "lineCount": package["line_count"]}
