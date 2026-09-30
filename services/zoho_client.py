from __future__ import annotations

import os
import time
import logging
from contextvars import ContextVar
from datetime import date, datetime

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("zoho")
REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
MAX_RETRIES = 3


class ZohoError(Exception):
    pass


_access_token: str | None = None
_access_token_expires_at = 0.0
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
    for attempt in range(MAX_RETRIES + 1):
        started = time.monotonic()
        try:
            response = httpx.request(
                method,
                url,
                headers={"Authorization": f"Zoho-oauthtoken {get_access_token()}"},
                params={"organization_id": _required("ZOHO_ORG_ID"), **params},
                timeout=REQUEST_TIMEOUT,
            )
            metrics = _request_metrics.get()
            if metrics is not None:
                metrics["api_calls"] = int(metrics.get("api_calls", 0)) + 1
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info("[ZOHO] endpoint=%s status=%s ms=%s attempt=%s", path, response.status_code, elapsed_ms, attempt + 1)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < MAX_RETRIES:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = min(10.0, max(0.25, float(retry_after))) if retry_after else min(8.0, 0.5 * (2 ** attempt))
                    except ValueError:
                        delay = min(8.0, 0.5 * (2 ** attempt))
                    time.sleep(delay)
                    continue
            if response.status_code in {401, 403}:
                raise ZohoError("Zoho connection needs re-authentication.")
            if response.status_code != 200:
                try:
                    message = response.json().get("message")
                except ValueError:
                    message = None
                raise ZohoError(f"Zoho Inventory returned HTTP {response.status_code}: {message or 'request rejected'}.")
            return response.json()
        except httpx.TimeoutException as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning("[ZOHO] endpoint=%s status=timeout ms=%s attempt=%s", path, elapsed_ms, attempt + 1)
            if attempt < MAX_RETRIES:
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                continue
            raise ZohoError("Zoho Inventory request timed out.") from exc
        except httpx.HTTPError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning("[ZOHO] endpoint=%s status=network_error ms=%s attempt=%s", path, elapsed_ms, attempt + 1)
            if attempt < MAX_RETRIES:
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
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
