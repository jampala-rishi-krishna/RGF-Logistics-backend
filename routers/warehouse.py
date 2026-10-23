from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth.dependencies import require_role

router = APIRouter(tags=["warehouse"])

WAREHOUSE_OR_ADMIN = require_role("warehouse", "admin")
WAREHOUSE_FIELDS = ("id", "ROWID", "name", "location", "zone")
EVENT_FIELDS = ("id", "ROWID", "warehouse_id", "event_type", "manifest_id", "status", "event_time")

# 2026-09-24 (Step 6): warehouse/warehouse_events dropped from Neon (not in the final
# kept-table list) - in-memory only now. warehouse_loading_checklists (a different, kept
# table) is unaffected - see routers/pipeline.py.

_WAREHOUSES: list[dict] = []  # nothing ever wrote a Warehouse row; static empty list
_EVENTS: list[dict] = []


def _public_fields(row: dict, fields: tuple[str, ...]) -> dict:
    return {key: row[key] for key in fields if key in row}


@router.get("/warehouses", dependencies=[Depends(WAREHOUSE_OR_ADMIN)])
def list_warehouses():
    return [_public_fields(row, WAREHOUSE_FIELDS) for row in _WAREHOUSES]


@router.get("/warehouse-events", dependencies=[Depends(WAREHOUSE_OR_ADMIN)])
def list_warehouse_events(warehouseId: str | None = None):
    if warehouseId:
        return [_public_fields(e, EVENT_FIELDS) for e in _EVENTS if e["warehouse_id"] == warehouseId]
    return [_public_fields(e, EVENT_FIELDS) for e in _EVENTS]


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
