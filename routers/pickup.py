from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from auth.dependencies import CurrentUser, require_manager, require_role
from database import get_db
from models.access_workflow import PickupDecision, PickupEmailLog
from services import purchase_orders, pickup_rules
from services.serialize import row_to_dict

router = APIRouter(prefix="/api/pickup", tags=["pickup"], dependencies=[Depends(require_role("admin", "dispatcher"))])


@router.get("/pahabol")
def pahabol():
    records, _ = purchase_orders.get_records()
    candidates = [r for r in records.values() if purchase_orders.is_wanted(r) and pickup_rules.classify(r) == "pahabol"]
    pending = purchase_orders.request_details(candidates)
    merged = [{**r, **(purchase_orders.cached_detail(str(r['purchaseorder_id']), r) or {})} for r in candidates]
    return {"items": [purchase_orders.summary(purchase_orders.build_row(r, purchase_orders.cached_detail(str(r['purchaseorder_id']), r))) for r in merged if purchase_orders.pickup_state(r) == "pahabol"], "details_pending": pending}


class DecisionBody(BaseModel):
    action: str


@router.post("/{order_id}/decision")
def decide(order_id: str, body: DecisionBody, user: CurrentUser = Depends(require_manager), db: Session = Depends(get_db)):
    if body.action not in {"approve", "reject"}:
        raise HTTPException(422, "Choose approve or reject")
    pid = purchase_orders.zoho_id(order_id)
    records, _ = purchase_orders.get_records()
    record = records.get(pid)
    if not record or not purchase_orders.is_wanted(record):
        raise HTTPException(404, "Purchase order not found")
    detail = purchase_orders.cached_detail(pid, record) or purchase_orders.fetch_detail(pid, record)
    merged = {**record, **detail}
    if pickup_rules.classify(merged) != "pahabol":
        raise HTTPException(409, "Order is no longer eligible for Pahabol approval")
    key = pickup_rules.decision_key(merged)
    # Serialize decisions on the existing user row; the unique key also protects multi-worker races.
    from models.user import User
    db.execute(select(User.id).where(User.id == user.id).with_for_update())
    row = db.execute(select(PickupDecision).where(PickupDecision.decision_key == key)).scalar_one_or_none()
    if row:
        raise HTTPException(409, "This PO already has a decision")
    status = "approved" if body.action == "approve" else "rejected"
    db.add(PickupDecision(decision_key=key, order_id=purchase_orders.row_id(pid), delivery_date=purchase_orders.delivery_day(merged)[0], status=status, decided_by=user.id, decided_by_name=user.full_name))
    from sqlalchemy.exc import IntegrityError
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This PO already has a decision") from None
    with purchase_orders._lock:
        purchase_orders._decisions[key] = status
    return {"status": status}


@router.get("/email/preview")
def preview(user: CurrentUser = Depends(require_manager)):
    from services import pickup_email
    return pickup_email.preview()


class SendBody(BaseModel):
    preview_token: str


@router.post("/email/send")
def send(body: SendBody, user: CurrentUser = Depends(require_manager), db: Session = Depends(get_db)):
    from services import pickup_email
    try:
        return pickup_email.send(db, preview_token=body.preview_token)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/email/logs")
def logs(user: CurrentUser = Depends(require_manager), db: Session = Depends(get_db)):
    return [row_to_dict(r) for r in db.execute(select(PickupEmailLog).order_by(PickupEmailLog.created_at.desc()).limit(100)).scalars()]


@router.get("/{order_id}/sheet.pdf")
def sheet(order_id: str):
    from services.pickup_email import documents, printable
    order = purchase_orders.ensure_row(order_id)
    pdf, _ = documents([printable(order)], [])
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="Pickup-sheet.pdf"'})
