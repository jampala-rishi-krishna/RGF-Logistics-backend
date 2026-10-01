from __future__ import annotations

import os
import time
import logging
import json
import random
import threading
from email.utils import parsedate_to_datetime
from uuid import uuid4
from contextvars import ContextVar
from datetime import date, datetime

import httpx
from dotenv import load_dotenv
from services import zoho_acquisition, zoho_rate_limiter

load_dotenv()

logger = logging.getLogger("zoho")
REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
MAX_RETRIES = 3
# Retry delays (unchanged from before Phase 2B): 0.5 * 2**attempt capped at 8 s, or Retry-After
# clamped to [0.25, 10] s. Phase 2B adds bounded upward jitter to the fallback delay only, so
# concurrent failures do not retry in lockstep. A provided Retry-After is honoured exactly.
RETRY_BASE_SECONDS = 0.5
RETRY_MAX_SECONDS = 8.0
RETRY_AFTER_MIN_SECONDS = 0.25
RETRY_AFTER_MAX_SECONDS = 10.0
RETRY_JITTER_FRACTION = 0.25


class ZohoError(Exception):
    pass


_access_token: str | None = None
_access_token_expires_at = 0.0
_token_lock = threading.Lock()  # single-flight OAuth refresh; Accounts endpoint, never paced as Inventory
_request_metrics: ContextVar[dict | None] = ContextVar("zoho_request_metrics", default=None)

def reset_api_call_count() -> None:
    _request_metrics.set({"api_calls": 0})

def begin_request_metrics() -> None:
    _request_metrics.set({"api_calls": 0})

def api_call_count() -> int:
    return int((_request_metrics.get() or {}).get("api_calls", 0))


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ZohoError("Zoho connection needs configuration.")
    return value


def get_access_token() -> str:
    if _access_token and time.time() < _access_token_expires_at - 60:
        return _access_token
    # Concurrent callers that all see an expired token wait for one refresh instead of each
    # calling Accounts. The lock covers only this timeout-bounded auth call, never Inventory I/O.
    with _token_lock:
        return _refresh_access_token()


def _refresh_access_token() -> str:
    global _access_token, _access_token_expires_at
    if _access_token and time.time() < _access_token_expires_at - 60:
        return _access_token
    try:
        response = httpx.post(
            f"{os.environ.get('ZOHO_ACCOUNTS_URL', 'https://accounts.zoho.com')}/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": _required("ZOHO_CLIENT_ID"),
                "client_secret": _required("ZOHO_CLIENT_SECRET"),
                "refresh_token": _required("ZOHO_REFRESH_TOKEN"),
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            raise ZohoError("Zoho connection needs re-authentication.")
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise ZohoError("Zoho connection needs re-authentication.")
        _access_token = token
        _access_token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        logger.info("[ZOHO] token refresh status=ok expires_in=%ss", int(payload.get("expires_in", 3600)))
        return token
    except (httpx.HTTPError, ValueError) as exc:
        raise ZohoError("Zoho connection is unavailable.") from exc


def _request(method: str, path: str, params: dict) -> dict:
    url = f"{os.environ.get('ZOHO_API_DOMAIN', 'https://www.zohoapis.com')}/inventory/v1/{path}"
    query = {"organization_id": _required("ZOHO_ORG_ID"), **params}
    if method == "GET":
        key = (method, url, os.environ.get("ZOHO_CLIENT_ID", ""), json.dumps(query, sort_keys=True))
        return zoho_acquisition.acquire(
            key, lambda logical_id: _request_http(method, path, url, query, logical_id),
            meta={"method": method, "endpoint": path},
            reusable=path.startswith("salesorders/") and path.count("/") == 1,
            valid=lambda body: isinstance(body, dict) and body.get("code", 0) == 0
            and isinstance(body.get("salesorder"), dict) and bool(body["salesorder"].get("salesorder_id")),
        )
    # Writes are never coalesced. Fence reads both before and after the existing
    # retry sequence, including an ambiguous failure whose server outcome is unknown.
    with zoho_acquisition.invalidation():
        pass
    try:
        logical_id = uuid4().hex
        zoho_acquisition.event("logical_request", logical_id=logical_id, method=method, endpoint=path)
        return _request_http(method, path, url, query, logical_id)
    finally:
        with zoho_acquisition.invalidation():
            pass


def _status_category(status: int) -> str:
    if status == 200:
        return "ok"
    if status == 429:
        return "rate_limited"
    if status in {401, 403}:
        return "auth"
    if status >= 500:
        return "server_error"
    return "client_error"


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse Retry-After as delta-seconds or an HTTP-date; None when missing or invalid."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        return (when - datetime.now(when.tzinfo)).total_seconds()
    except (TypeError, ValueError):
        return None


def _fallback_delay(attempt: int) -> float:
    base = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** attempt))
    return base + random.uniform(0, base * RETRY_JITTER_FRACTION)


def _request_http(method: str, path: str, url: str, query: dict, logical_id: str) -> dict:
    """Every actual Zoho Inventory HTTP attempt - first try and each retry - goes through the
    shared org limiter. Backoff sleeps happen outside the limiter, so a waiting retry holds no
    concurrency slot. The retry policy (what is retried, and MAX_RETRIES) is unchanged."""
    limiter = zoho_rate_limiter.limiter_for(query.get("organization_id", ""))
    for attempt in range(MAX_RETRIES + 1):
        started = time.monotonic()
        try:
            token = get_access_token()  # Accounts endpoint: deliberately not gated as Inventory
            with limiter.admit() as ticket:
                started = time.monotonic()
                metrics = _request_metrics.get()
                if metrics is not None:
                    metrics["api_calls"] = int(metrics.get("api_calls", 0)) + 1
                zoho_acquisition.event("http_attempt", logical_id=logical_id, method=method,
                                       endpoint=path, attempt=attempt + 1, retry=int(attempt > 0),
                                       page=query.get("page"), per_page=query.get("per_page"),
                                       rate_wait_ms=ticket.rate_wait_ms, concurrency_wait_ms=ticket.concurrency_wait_ms)
                response = httpx.request(
                    method,
                    url,
                    headers={"Authorization": f"Zoho-oauthtoken {token}"},
                    params=query,
                    timeout=REQUEST_TIMEOUT,
                )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info("[ZOHO] endpoint=%s status=%s ms=%s attempt=%s", path, response.status_code, elapsed_ms, attempt + 1)
            zoho_acquisition.event("http_outcome", logical_id=logical_id, endpoint=path, attempt=attempt + 1,
                                   status=response.status_code, category=_status_category(response.status_code),
                                   ms=elapsed_ms, retry_after=response.headers.get("Retry-After") or "none")
            if response.status_code == 429 or response.status_code >= 500:
                retry_after_raw = response.headers.get("Retry-After")
                retry_after = _retry_after_seconds(retry_after_raw)
                if response.status_code == 429:
                    zoho_acquisition.event("http_429", logical_id=logical_id, endpoint=path, attempt=attempt + 1,
                                           retry_after=retry_after_raw or "missing")
                if attempt < MAX_RETRIES:
                    if retry_after is not None:
                        delay = min(RETRY_AFTER_MAX_SECONDS, max(RETRY_AFTER_MIN_SECONDS, retry_after))
                    else:
                        delay = _fallback_delay(attempt)
                    if response.status_code == 429:
                        # Organization-wide backpressure: no caller starts a new attempt during the wait.
                        limiter.penalize(delay)
                    zoho_acquisition.event("http_retry", logical_id=logical_id, endpoint=path, attempt=attempt + 1,
                                           status=response.status_code, delay_s=round(delay, 3),
                                           retry_after=retry_after_raw or "missing")
                    _sleep(delay)
                    continue
            if response.status_code in {401, 403}:
                raise ZohoError("Zoho connection needs re-authentication.")
            if response.status_code != 200:
                zoho_acquisition.event("http_failure", logical_id=logical_id, endpoint=path,
                                       attempt=attempt + 1, status=response.status_code)
                try:
                    message = response.json().get("message")
                except ValueError:
                    message = None
                raise ZohoError(f"Zoho Inventory returned HTTP {response.status_code}: {message or 'request rejected'}.")
            payload = response.json()
            rows = sum(len(value) for value in payload.values() if isinstance(value, list)) if isinstance(payload, dict) else 0
            has_more = (payload.get("page_context") or {}).get("has_more_page") if isinstance(payload, dict) and isinstance(payload.get("page_context"), dict) else None
            zoho_acquisition.event("http_success", logical_id=logical_id)
            zoho_acquisition.event("http_result", logical_id=logical_id, endpoint=path,
                                   attempt=attempt + 1, status=response.status_code, rows=rows, has_more_page=has_more, ms=elapsed_ms)
            return payload
        except httpx.TimeoutException as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning("[ZOHO] endpoint=%s status=timeout ms=%s attempt=%s", path, elapsed_ms, attempt + 1)
            zoho_acquisition.event("http_outcome", logical_id=logical_id, endpoint=path, attempt=attempt + 1,
                                   status="timeout", category="timeout", ms=elapsed_ms, retry_after="none")
            if attempt < MAX_RETRIES:
                delay = _fallback_delay(attempt)
                zoho_acquisition.event("http_retry", logical_id=logical_id, endpoint=path, attempt=attempt + 1,
                                       status="timeout", delay_s=round(delay, 3))
                _sleep(delay)
                continue
            raise ZohoError("Zoho Inventory request timed out.") from exc
        except httpx.HTTPError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning("[ZOHO] endpoint=%s status=network_error ms=%s attempt=%s", path, elapsed_ms, attempt + 1)
            zoho_acquisition.event("http_outcome", logical_id=logical_id, endpoint=path, attempt=attempt + 1,
                                   status="network_error", category="network", ms=elapsed_ms, retry_after="none")
            if attempt < MAX_RETRIES:
                delay = _fallback_delay(attempt)
                zoho_acquisition.event("http_retry", logical_id=logical_id, endpoint=path, attempt=attempt + 1,
                                       status="network_error", delay_s=round(delay, 3))
                _sleep(delay)
                continue
            raise ZohoError("Zoho Inventory is unavailable.") from exc


def fetch_sales_orders(
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = 1,
    per_page: int = 200,
    filter_by: str | None = None,
    sort_column: str | None = None,
) -> dict:
    params: dict = {"page": page, "per_page": min(per_page, 200)}
    if date_from and date_to:
        params.update({"date_start": date_from.isoformat(), "date_end": date_to.isoformat()})
    if filter_by:
        params["filter_by"] = filter_by
    if sort_column:
        params["sort_column"] = sort_column
    return _request("GET", "salesorders", params)


def fetch_sales_order_detail(salesorder_id: str) -> dict:
    return _request("GET", f"salesorders/{salesorder_id}", {})


def fetch_item_detail(item_id: str) -> dict:
    """Fetch structured Zoho item/package data for weight calculations."""
    return _request("GET", f"items/{item_id}", {})


def fetch_sales_orders_by_customview(customview_id: str, page: int = 1, per_page: int = 200) -> dict:
    """List sales orders exactly as a saved Zoho Custom View would (e.g. Acknowledged),
    delegating the filtering logic to Zoho instead of reconstructing it locally."""
    return _request("GET", "salesorders", {"customview_id": customview_id, "page": page, "per_page": min(per_page, 200)})

def fetch_sales_orders_by_shipment_date(start: date, end: date, page: int = 1, per_page: int = 200) -> dict:
    """Zoho shipment-date list filter. The caller still filters locally because
    Zoho tenants vary in whether this filter is honored."""
    return _request("GET", "salesorders", {"shipment_date_start": start.isoformat(), "shipment_date_end": end.isoformat(), "page": page, "per_page": min(per_page, 200)})


def acknowledge_sales_order(salesorder_id: str, status_code: str = "cs_acknowl") -> dict:
    """Apply Zoho's custom acknowledgement sub-status to a sales order."""
    return _request("POST", f"salesorders/{salesorder_id}/substatus/{status_code}", {})


def remove_acknowledge_sales_order(salesorder_id: str) -> dict:
    """Reset the sales order sub-status to Zoho's plain Confirmed state."""
    return _request("POST", f"salesorders/{salesorder_id}/substatus/confirmed", {})


def confirm_sales_order(salesorder_id: str) -> dict:
    """Revert an acknowledged order to Zoho's supported Confirmed state."""
    return _request("POST", f"salesorders/{salesorder_id}/status/confirmed", {})


def fetch_packages(
    page: int = 1,
    per_page: int = 200,
    filter_by: str | None = None,
    sort_column: str | None = None,
    shipment_date_start: date | None = None,
    shipment_date_end: date | None = None,
) -> dict:
    params: dict = {"page": page, "per_page": min(per_page, 200)}
    if filter_by:
        params["filter_by"] = filter_by
    if sort_column:
        params["sort_column"] = sort_column
    if shipment_date_start:
        params["shipment_date_start"] = shipment_date_start.isoformat()
    if shipment_date_end:
        params["shipment_date_end"] = shipment_date_end.isoformat()
    return _request("GET", "packages", params)


def fetch_package_detail(package_id: str) -> dict:
    return _request("GET", f"packages/{package_id}", {})


def fetch_transfer_orders(page: int = 1, per_page: int = 200, sort_column: str | None = None) -> dict:
    params: dict = {"page": page, "per_page": min(per_page, 200)}
    if sort_column:
        params["sort_column"] = sort_column
    return _request("GET", "transferorders", params)


def fetch_inventory_adjustments(page: int = 1, per_page: int = 200, sort_column: str | None = None) -> dict:
    params: dict = {"page": page, "per_page": min(per_page, 200)}
    if sort_column:
        params["sort_column"] = sort_column
    return _request("GET", "inventoryadjustments", params)


def fetch_invoices(page: int = 1, per_page: int = 200, filter_by: str | None = None, sort_column: str | None = None) -> dict:
    params: dict = {"page": page, "per_page": min(per_page, 200)}
    if filter_by:
        params["filter_by"] = filter_by
    if sort_column:
        params["sort_column"] = sort_column
    return _request("GET", "invoices", params)


def fetch_purchase_receives(page: int = 1, per_page: int = 200, sort_column: str | None = None) -> dict:
    params: dict = {"page": page, "per_page": min(per_page, 200)}
    if sort_column:
        params["sort_column"] = sort_column
    return _request("GET", "purchasereceives", params)


def fetch_purchase_receive_detail(purchasereceive_id: str) -> dict:
    return _request("GET", f"purchasereceives/{purchasereceive_id}", {})
