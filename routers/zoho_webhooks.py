from __future__ import annotations

import os
import threading
import time
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from auth.dependencies import require_role
from services import live_sales_order_cache

router = APIRouter(prefix="/api/zoho/webhooks", tags=["zoho-webhooks"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])

_lock = threading.Lock()
_seen: dict[str, float] = {}
_TTL_SECONDS = 24 * 60 * 60
_CAPTURE_MAX_PER_ROUTE = 5
_CAPTURE_MAX_BY_ROUTE = {"salesorder": 15}
_captures: dict[str, list[dict]] = {}

_SENSITIVE_EXACT = {
    "customer_name", "customername", "contact_name", "contactname", "first_name", "firstname",
    "last_name", "lastname", "phone", "phone_number", "mobile", "mobile_number", "email",
    "email_address", "street", "street_address", "address", "address_line", "address_line1",
    "address_line2", "address_line_1", "address_line_2", "notes", "note", "comments", "comment",
}


def _enabled() -> bool:
    return os.environ.get("ZOHO_WEBHOOKS_ENABLED", "false").strip().lower() == "true"


def _secret() -> str:
    return os.environ.get("ZOHO_WEBHOOK_SECRET", "").strip()


def _capture_enabled() -> bool:
    return os.environ.get("ZOHO_WEBHOOK_CAPTURE", "false").strip().lower() == "true"


def _verify_secret(header_secret: str | None, query_key: str | None) -> None:
    expected = _secret()
    supplied = header_secret if header_secret is not None else query_key
    if not expected or supplied is None or not hmac.compare_digest(str(supplied), expected):
        raise HTTPException(401, "Invalid webhook secret.")


def _redact_key(key: str) -> bool:
    normalized = "".join(ch for ch in str(key).casefold() if ch.isalnum() or ch == "_")
    if normalized in _SENSITIVE_EXACT:
        return True
    return (
        normalized.endswith(("_email", "_phone", "_mobile"))
        or normalized.startswith(("customer_", "contact_")) and normalized.endswith(("name", "person"))
        or "street" in normalized and "name" not in normalized
        or "address" in normalized and not normalized.endswith("_id")
        or normalized.startswith(("note_", "comment_"))
    )


def _redact_private(value):
    if isinstance(value, dict):
        return {key: _redact_private(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_private(item) for item in value]
    return "[REDACTED]"


def _redact(value):
    if isinstance(value, dict):
        return {key: _redact_private(item) if _redact_key(str(key)) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


async def _parse_body(request: Request):
    raw = await request.body()
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if "json" in media_type:
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw.decode("utf-8", "replace")
    if media_type == "application/x-www-form-urlencoded":
        return {key: value for key, value in parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True)}
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", "replace")


def _safe_headers(request: Request) -> dict[str, str]:
    blocked = {"authorization", "cookie", "set-cookie", "x-zoho-webhook-secret"}
    return {key: value for key, value in request.headers.items() if key.casefold() not in blocked}


def _store_capture(route: str, request: Request, body) -> None:
    capture = {
        "content_type": request.headers.get("content-type", ""),
        "headers": _safe_headers(request),
        "body": _redact(body),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "route": route,
    }
    with _lock:
        bucket = _captures.setdefault(route, [])
        if len(bucket) < _CAPTURE_MAX_BY_ROUTE.get(route, _CAPTURE_MAX_PER_ROUTE):
            bucket.append(capture)


async def _capture_or_none(route: str, request: Request, header_secret: str | None, query_key: str | None):
    _verify_secret(header_secret, query_key)
    if not _capture_enabled():
        return None
    body = await _parse_body(request)
    _store_capture(route, request, body)
    return {"captured": True}


@admin_router.get("/zoho-webhook-captures")
def get_zoho_webhook_captures():
    with _lock:
        return {
            route: [{key: value for key, value in capture.items() if key != "route"} for capture in captures]
            for route, captures in _captures.items()
        }


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
async def salesorder_webhook(request: Request, key: str | None = None, x_zoho_webhook_secret: str | None = Header(None)):
    captured = await _capture_or_none("salesorder", request, x_zoho_webhook_secret, key)
    if captured is not None:
        return captured
    if not _enabled():
        raise HTTPException(404, "Zoho webhooks are disabled.")
    _verify_secret(x_zoho_webhook_secret, key)
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


def _capture_route(route: str):
    async def handler(request: Request, key: str | None = None, x_zoho_webhook_secret: str | None = Header(None)):
        captured = await _capture_or_none(route, request, x_zoho_webhook_secret, key)
        if captured is not None:
            return captured
        if not _enabled():
            raise HTTPException(404, "Zoho webhooks are disabled.")
        # Phase 1 applies these routes; capture mode is deliberately the only
        # behavior enabled here until their real payloads are captured.
        return {"ignored": True}
    handler.__name__ = f"{route}_webhook"
    return handler


for _route in ("purchaseorder", "transferorder", "package", "lock-result"):
    router.add_api_route(f"/{_route}", _capture_route(_route), methods=["POST"])
