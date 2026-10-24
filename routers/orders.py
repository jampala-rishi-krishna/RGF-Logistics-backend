from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from auth.dependencies import CurrentUser, require_role
from services import memory_tables

router = APIRouter(prefix="/orders", tags=["orders"])

# Role gates (2026-10-12): reads = admin, dispatcher, warehouse, client; writes (/events, /proof) = admin, dispatcher, warehouse.
#
# PORTAL / client: these in-memory orders carry a customer_id but a `users` row has NO customer link yet, so a client
# login cannot be scoped to "their" orders. Until that link exists a client gets [] from the list and 404 from the
# detail (the `customerId` query parameter is ignored for clients - it is caller-controlled). Never another customer's data.
# Nothing in the codebase creates memory_tables.orders rows either (the store is always empty today).
#
# 2026-09-24 (Step 6): orders/customers/order_events/delivery_proofs dropped from Neon (not
# in the final kept-table list) - in-memory only now. This is the customer-tracking Portal
# feature (App.tsx's Portal component, "tracking" page) - a separate, minimally-used
# subsystem from the dispatcher's Sales Order / Confirmed SO flow (which is Zoho-live, see
# routers/load_planning.py). Both tables were already empty (0 rows) before this change.

PRIVATE_MEDIA_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "private_media", "delivery_proofs")
MAX_PROOF_BYTES = 10 * 1024 * 1024
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


READ_ROLES = Depends(require_role("admin", "dispatcher", "warehouse", "client"))
WRITE_ROLES = Depends(require_role("admin", "dispatcher", "warehouse"))


@router.get("")
def list_orders(customerId: str | None = None, user: CurrentUser = READ_ROLES):
    if user.role == "client":
        return []
    return memory_tables.orders.list(customer_id=customerId)


@router.get("/{order_id}")
def get_order(order_id: int, user: CurrentUser = READ_ROLES):
    if user.role == "client":
        raise HTTPException(404, "Order not found")
    order = memory_tables.orders.get(order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    return order


class OrderEventBody(BaseModel):
    eventType: str
    notes: str | None = None


@router.post("/{order_id}/events", status_code=201, dependencies=[WRITE_ROLES])
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


@router.post("/{order_id}/proof", status_code=201, dependencies=[WRITE_ROLES])
async def post_delivery_proof(
    order_id: int,
    file: UploadFile = File(...),
    signatureRef: str | None = Form(None),
    recipientName: str | None = Form(None),
):
    if memory_tables.orders.get(order_id) is None:
        raise HTTPException(404, "Order not found")

    # Proofs are private operational records. Keep them outside the public /media mount and
    # expose them only through get_delivery_proof(), which checks the order authorization.
    order_dir = os.path.join(PRIVATE_MEDIA_ROOT, str(order_id))
    os.makedirs(order_dir, exist_ok=True)
    raw_name = (file.filename or "proof.bin").replace("\\", "/")
    safe_name = _SAFE_FILENAME.sub("_", os.path.basename(raw_name)) or "proof.bin"
    filename = f"{uuid.uuid4().hex}-{safe_name}"
    dest_path = os.path.join(order_dir, filename)
    data = await file.read(MAX_PROOF_BYTES + 1)
    if len(data) > MAX_PROOF_BYTES:
        raise HTTPException(413, f"Proof file must be {MAX_PROOF_BYTES // (1024 * 1024)} MB or smaller.")
    with open(dest_path, "wb") as f:
        f.write(data)

    proof = memory_tables.delivery_proofs.create(
        order_id=str(order_id),
        photo_url="",
        storage_path=dest_path,
        content_type=file.content_type or "application/octet-stream",
        signature_ref=signatureRef or "",
        recipient_name=recipientName or "",
        captured_at=datetime.now(timezone.utc),
    )
    return memory_tables.delivery_proofs.update(
        proof["id"], photo_url=f"/orders/{order_id}/proof/{proof['id']}"
    )


@router.get("/{order_id}/proof/{proof_id}")
def get_delivery_proof(order_id: int, proof_id: int, user: CurrentUser = READ_ROLES):
    """Stream a delivery proof only after verifying its order association and caller role."""
    if user.role == "client":
        raise HTTPException(404, "Proof not found")
    if memory_tables.orders.get(order_id) is None:
        raise HTTPException(404, "Proof not found")
    proof = memory_tables.delivery_proofs.get(proof_id)
    if proof is None or str(proof.get("order_id")) != str(order_id):
        raise HTTPException(404, "Proof not found")
    stored = proof.get("storage_path")
    if not stored:
        raise HTTPException(404, "Proof file not found")
    root = Path(PRIVATE_MEDIA_ROOT).resolve()
    path = Path(stored).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "Proof file not found")
    return FileResponse(path, media_type=proof.get("content_type") or "application/octet-stream")
