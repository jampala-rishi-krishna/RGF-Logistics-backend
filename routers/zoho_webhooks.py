from __future__ import annotations

import os
import threading
import time
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from auth.dependencies import require_role
from services import live_sales_order_cache, zoho_usage

router = APIRouter(prefix="/api/zoho/webhooks", tags=["zoho-webhooks"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])

_lock = threading.Lock()
_seen: dict[str, float] = {}
_TTL_SECONDS = 24 * 60 * 60
_CAPTURE_MAX_PER_ROUTE = 5
_CAPTURE_MAX_BY_ROUTE = {"salesorder": 15}
_captures: dict[str, list[dict]] = {}
_apply_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="zoho-webhook")
_pending_lock = threading.Lock()
_pending: dict[tuple[str, str], dict] = {}
_pending_timers: dict[tuple[str, str], threading.Timer] = {}
DEBOUNCE_SECONDS = float(os.environ.get("ZOHO_WEBHOOK_DEBOUNCE_SECONDS", "30"))

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
    supplied = [value for value in (header_secret, query_key) if value is not None]
    valid = any(hmac.compare_digest(str(value), expected) for value in supplied) if expected else False
    if not valid:
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
    zoho_usage.record_webhook(route)
    if not _capture_enabled():
        return None
    body = await _parse_body(request)
    _store_capture(route, request, body)
    return {"captured": True}


def _record_for_route(route: str, body):
    if not isinstance(body, dict):
        return body
    key = {"salesorder": "salesorder", "purchaseorder": "purchaseorder", "transferorder": "transfer_order"}.get(route)
    if key and isinstance(body.get(key), dict):
        return body[key]
    return body


def _entity_id(route: str, record: dict) -> str:
    keys = {
        "salesorder": ("salesorder_id", "sales_order_id", "id"),
        "purchaseorder": ("purchaseorder_id", "purchase_order_id", "id"),
        "transferorder": ("transfer_order_id", "transferorder_id", "id"),
        "lock-result": ("salesorder_id", "sales_order_id", "id"),
        "package": ("package_id", "salesorder_id", "id"),
    }.get(route, ("id",))
    return next((str(record.get(key)) for key in keys if record.get(key) not in (None, "")), "")


def _apply(route: str, record: dict) -> bool:
    if route == "salesorder":
        return live_sales_order_cache.apply_salesorder_webhook(record)
    if route == "purchaseorder":
        from services import purchase_orders
        return purchase_orders.apply_webhook(record)
    if route == "transferorder":
        from services import transfer_orders
        return transfer_orders.apply_webhook(record)
    if route == "lock-result":
        from services import zoho_so_lock
        zoho_so_lock.receive_lock_result(record)
        return live_sales_order_cache.apply_lock_webhook(record)
    # Shipment data is carried by the SO webhook's packages[] field. The package
    # endpoint remains a compatibility receiver and intentionally causes no Zoho read.
    return False


def _flush_key(key: tuple[str, str]) -> None:
    with _pending_lock:
        record = _pending.pop(key, None)
        _pending_timers.pop(key, None)
    if record is not None:
        _apply_pool.submit(_apply, key[0], record)


def _queue_apply(route: str, record: dict) -> tuple[str, str]:
    entity = _entity_id(route, record) or f"event:{time.monotonic_ns()}"
    key = (route, entity)
    with _pending_lock:
        old = _pending.get(key)
        old_modified = str((old or {}).get("last_modified_time") or "")
        new_modified = str(record.get("last_modified_time") or "")
        if old is None or not old_modified or not new_modified or new_modified > old_modified:
            _pending[key] = record
        timer = _pending_timers.get(key)
        if timer is None or not timer.is_alive():
            timer = threading.Timer(DEBOUNCE_SECONDS, _flush_key, args=(key,))
            timer.daemon = True
            _pending_timers[key] = timer
            timer.start()
    return key


def flush_pending_webhooks() -> None:
    """Test/support hook: apply all debounced events immediately."""
    with _pending_lock:
        keys = list(_pending)
        timers = [t for t in _pending_timers.values() if t.is_alive()]
    for timer in timers:
        timer.cancel()
    futures = []
    for key in keys:
        with _pending_lock:
            record = _pending.pop(key, None)
            _pending_timers.pop(key, None)
        if record is not None:
            futures.append(_apply_pool.submit(_apply, key[0], record))
    for future in futures:
        future.result(timeout=5)


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
        return {"ignored": True}
    body = await request.json()
    record = _record_for_route("salesorder", body)
    if not isinstance(record, dict):
        raise HTTPException(400, "Expected a salesorder object.")
    order_id = str(record.get("salesorder_id") or record.get("sales_order_id") or record.get("id") or "")
    modified = str(record.get("last_modified_time") or record.get("event_time") or "")
    event_id = str(body.get("event_id") or body.get("webhook_id") or f"{order_id}:{modified}")
    if not order_id:
        raise HTTPException(400, "salesorder_id is required.")
    if not _remember_once(event_id):
        return {"ok": True, "duplicate": True, "salesorder_id": order_id}
    _queue_apply("salesorder", record)
    return {"ok": True, "queued": True, "updated": True, "salesorder_id": order_id}


def _capture_route(route: str):
    async def handler(request: Request, key: str | None = None, x_zoho_webhook_secret: str | None = Header(None)):
        captured = await _capture_or_none(route, request, x_zoho_webhook_secret, key)
        if captured is not None:
            return captured
        if not _enabled():
            return {"ignored": True}
        body = await request.json()
        record = _record_for_route(route, body)
        if not isinstance(record, dict):
            raise HTTPException(400, f"Expected a {route} object.")
        if route == "package":
            return {"ok": True, "ignored": True, "reason": "packages are applied from salesorder payloads"}
        _queue_apply(route, record)
        return {"ok": True, "queued": True, "id": _entity_id(route, record)}
    handler.__name__ = f"{route}_webhook"
    return handler


for _route in ("purchaseorder", "transferorder", "package", "lock-result"):
    router.add_api_route(f"/{_route}", _capture_route(_route), methods=["POST"])
