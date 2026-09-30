from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth.dependencies import require_role

router = APIRouter(tags=["warehouse"])

WAREHOUSE_OR_ADMIN = require_role("warehouse", "admin")

# 2026-09-24 (Step 6): warehouse/warehouse_events dropped from Neon (not in the final
# kept-table list) - in-memory only now. warehouse_loading_checklists (a different, kept
# table) is unaffected - see routers/pipeline.py.

_WAREHOUSES: list[dict] = []  # nothing ever wrote a Warehouse row; static empty list
_EVENTS: list[dict] = []


@router.get("/warehouses")
def list_warehouses():
    return _WAREHOUSES


@router.get("/warehouse-events")
def list_warehouse_events(warehouseId: str | None = None):
    if warehouseId:
        return [e for e in _EVENTS if e["warehouse_id"] == warehouseId]
    return list(_EVENTS)


class WarehouseEventBody(BaseModel):
    warehouseId: str
    eventType: str
    manifestId: str | None = None
    status: str | None = None


@router.post("/warehouse-events", status_code=201, dependencies=[Depends(WAREHOUSE_OR_ADMIN)])
def create_warehouse_event(body: WarehouseEventBody):
    event = {
        "id": len(_EVENTS) + 1,
        "warehouse_id": str(body.warehouseId),
        "event_type": body.eventType,
        "manifest_id": str(body.manifestId) if body.manifestId else "",
        "status": body.status or "pending",
        "event_time": datetime.now(timezone.utc),
    }
    _EVENTS.append(event)
    return event
