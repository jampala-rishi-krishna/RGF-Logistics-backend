from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from services import memory_tables

router = APIRouter(prefix="/orders", tags=["orders"])

# orders-module/index.js enforces no role gate on any route and writes no audit_log entries -
# ported faithfully (confirmed pre-existing gap, not newly introduced here).
#
# 2026-09-24 (Step 6): orders/customers/order_events/delivery_proofs dropped from Neon (not
# in the final kept-table list) - in-memory only now. This is the customer-tracking Portal
# feature (App.tsx's Portal component, "tracking" page) - a separate, minimally-used
# subsystem from the dispatcher's Sales Order / Confirmed SO flow (which is Zoho-live, see
# routers/load_planning.py). Both tables were already empty (0 rows) before this change.

MEDIA_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media", "delivery_proofs")


@router.get("")
def list_orders(customerId: str | None = None):
    return memory_tables.orders.list(customer_id=customerId)


@router.get("/{order_id}")
def get_order(order_id: int):
    order = memory_tables.orders.get(order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    return order


class OrderEventBody(BaseModel):
    eventType: str
    notes: str | None = None


@router.post("/{order_id}/events", status_code=201)
def post_order_event(order_id: int, body: OrderEventBody):
    if memory_tables.orders.get(order_id) is None:
        raise HTTPException(404, "Order not found")
    if not body.eventType:
        raise HTTPException(400, "eventType is required")
    return memory_tables.order_events.create(
        order_id=str(order_id),
        event_type=body.eventType,
        event_time=datetime.now(timezone.utc),
        notes=body.notes or "",
    )


@router.post("/{order_id}/proof", status_code=201)
async def post_delivery_proof(
    order_id: int,
    file: UploadFile = File(...),
    signatureRef: str | None = Form(None),
    recipientName: str | None = Form(None),
):
    if memory_tables.orders.get(order_id) is None:
        raise HTTPException(404, "Order not found")

    # Original stored this in a Zoho Stratus bucket with a 7-day signed URL - there's no
    # equivalent managed object store configured for this migration, so proofs are saved to
    # local disk under /media and served back via main.py's static mount instead.
    order_dir = os.path.join(MEDIA_ROOT, str(order_id))
    os.makedirs(order_dir, exist_ok=True)
    filename = f"{int(time.time() * 1000)}-{file.filename}"
    dest_path = os.path.join(order_dir, filename)
    with open(dest_path, "wb") as f:
        f.write(await file.read())

    photo_url = f"/media/delivery_proofs/{order_id}/{filename}"

    return memory_tables.delivery_proofs.create(
        order_id=str(order_id),
        photo_url=photo_url,
        signature_ref=signatureRef or "",
        recipient_name=recipientName or "",
        captured_at=datetime.now(timezone.utc),
    )
