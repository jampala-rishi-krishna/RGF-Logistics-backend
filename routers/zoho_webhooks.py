from __future__ import annotations

import os
import threading
import time

from fastapi import APIRouter, Header, HTTPException, Request

from services import live_sales_order_cache

router = APIRouter(prefix="/api/zoho/webhooks", tags=["zoho-webhooks"])

_lock = threading.Lock()
_seen: dict[str, float] = {}
_TTL_SECONDS = 24 * 60 * 60


def _enabled() -> bool:
    return os.environ.get("ZOHO_WEBHOOKS_ENABLED", "false").strip().lower() == "true"


def _secret() -> str:
    return os.environ.get("ZOHO_WEBHOOK_SECRET", "").strip()


def _remember_once(key: str) -> bool:
    now = time.monotonic()
    with _lock:
        for existing, at in list(_seen.items()):
            if now - at > _TTL_SECONDS:
                _seen.pop(existing, None)
        if key in _seen:
            return False
        _seen[key] = now
        return True


@router.post("/salesorder")
async def salesorder_webhook(request: Request, x_zoho_webhook_secret: str | None = Header(None)):
    if not _enabled():
        raise HTTPException(404, "Zoho webhooks are disabled.")
    expected = _secret()
    if not expected or x_zoho_webhook_secret != expected:
        raise HTTPException(401, "Invalid webhook secret.")
    body = await request.json()
    record = body.get("salesorder") if isinstance(body, dict) and isinstance(body.get("salesorder"), dict) else body
    if not isinstance(record, dict):
        raise HTTPException(400, "Expected a salesorder object.")
    order_id = str(record.get("salesorder_id") or record.get("sales_order_id") or record.get("id") or "")
    modified = str(record.get("last_modified_time") or record.get("event_time") or "")
    event_id = str(body.get("event_id") or body.get("webhook_id") or f"{order_id}:{modified}")
    if not order_id:
        raise HTTPException(400, "salesorder_id is required.")
    if not _remember_once(event_id):
        return {"ok": True, "duplicate": True, "salesorder_id": order_id}
    updated = live_sales_order_cache.apply_salesorder_webhook(record)
    return {"ok": True, "duplicate": False, "updated": updated, "salesorder_id": order_id}
