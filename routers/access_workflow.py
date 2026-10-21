from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from auth.dependencies import CurrentUser, get_current_user, require_manager
from database import get_db
from models.access_workflow import UnassignRequest
from services import live_sales_order_cache
from services.serialize import row_to_dict

router = APIRouter(prefix="/api/access", tags=["access"])


class RequestBody(BaseModel):
    order_ids: list[str] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/unassign-requests", status_code=201)
def request_unassign(body: RequestBody, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in {"admin", "planner", "dispatcher"}:
        raise HTTPException(403, "Operational access required")
    if not body.reason.strip():
        raise HTTPException(422, "Reason is required")
    orders = []
    for oid in dict.fromkeys(body.order_ids):
        order = live_sales_order_cache.find_cached(oid)
        if order is None or order.assignment_status != "assigned":
            raise HTTPException(409, "Every order must still be assigned")
        orders.append(order)
    for order in orders:
        db.add(UnassignRequest(order_id=order.id, order_number=order.salesorder_number or order.id, requester_id=user.id,
            requester_name=user.full_name, reason=body.reason.strip(), status="pending", manager_reads=[], result_read=False))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A pending request already exists for one of these orders") from None
    return {"created": len(orders)}


@router.get("/alerts")
def alerts(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    own = (UnassignRequest.requester_id == user.id) & (UnassignRequest.status != "pending")
    predicate = or_(own, UnassignRequest.status == "pending") if user.is_manager else own
    rows = db.execute(select(UnassignRequest).where(predicate).order_by(UnassignRequest.created_at.desc())).scalars().all()
    items = []
    for row in rows:
        pending = row.status == "pending"
        unread = user.id not in row.manager_reads if pending else not row.result_read
        text = f"{row.requester_name} requests unassign: {row.order_number} ({row.reason})" if pending else f"{row.order_number}: {'Approved' if row.status == 'approved' else 'Rejected'}" + (f": {row.note}" if row.note else "")
        order = live_sales_order_cache.find_cached(row.order_id)
        day = getattr(order, "expected_shipment_date", None)
        items.append({**row_to_dict(row), "unread": unread, "text": text, "delivery_date": day.isoformat() if day else None})
    if user.is_manager:  # automatic unassigns after the crew had started (Zoho date change): process memory, no table (services/auto_unassign)
        from services import auto_unassign
        items = auto_unassign.manager_alerts(user.id) + items
    return {"items": items, "unread_count": sum(i["unread"] for i in items)}


@router.post("/alerts/{request_id}/read")
def read_alert(request_id: int, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    if request_id < 0:  # an in-memory automatic-unassign alert (services/auto_unassign): no database write
        from services import auto_unassign
        if not user.is_manager or not auto_unassign.mark_alert_read(request_id, user.id):
            raise HTTPException(404, "Alert not found")
        return {"ok": True}
    row = db.get(UnassignRequest, request_id)
    if row is None:
        raise HTTPException(404, "Request not found")
    if row.status == "pending" and user.is_manager:
        row.manager_reads = list(set(row.manager_reads + [user.id]))
    elif row.requester_id == user.id:
        row.result_read = True
    else:
        raise HTTPException(403, "Forbidden")
    db.commit()
    return {"ok": True}


class ResolveBody(BaseModel):
    action: str
    note: str | None = Field(default=None, max_length=2000)
    confirm: bool = False


@router.post("/unassign-requests/{request_id}/resolve")
def resolve(request_id: int, body: ResolveBody, user: CurrentUser = Depends(require_manager), db: Session = Depends(get_db)):
    row = db.execute(select(UnassignRequest).where(UnassignRequest.id == request_id).with_for_update()).scalar_one_or_none()
    if row is None or row.status != "pending":
        raise HTTPException(409, "Request is no longer pending")
    if body.action not in {"approve", "reject"}:
        raise HTTPException(422, "Choose approve or reject")
    if body.action == "approve":
        from routers import assignment
        from services import dispatch_changes
        order = live_sales_order_cache.find_cached(row.order_id)
        if order is None or order.assignment_status != "assigned":
            raise HTTPException(409, "Order is no longer assigned")
        candidate = dispatch_changes.describe(db, [assignment._candidate(order.id, order, require_assigned=True)])[0]
        if candidate["blocked"]:
            raise HTTPException(409, candidate["reason"])
        if not body.confirm:
            dispatch_changes.confirm_required([candidate])
        assignment._apply_unassign(order, db)
        assignment._record_unassign(db, user, candidate)
    row.status = "approved" if body.action == "approve" else "rejected"
    row.resolved_by, row.resolved_at, row.note = user.id, datetime.now(timezone.utc), body.note
    row.result_read = False
    db.commit()
    if body.action == "approve":
        assignment._after_unassign()
    return {"ok": True, "status": row.status}
