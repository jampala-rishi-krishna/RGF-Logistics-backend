from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

import httpx

from services import zoho_rate_limiter, zoho_usage
from services.zoho_client import REQUEST_TIMEOUT, ZohoError

logger = logging.getLogger("zoho")

_access_token: str | None = None
_access_token_expires_at = 0.0
_token_lock = threading.Lock()


def mode() -> str:
    """"webhook": lock through the IT-provided Zoho incoming webhook. Anything else: the direct lock API
    ("api", the fallback)."""
    return "webhook" if os.environ.get("ZOHO_SO_LOCK_MODE", "").strip().lower() == "webhook" else "api"


def configured() -> bool:
    # The lock credential is still needed in both modes: webhook mode verifies the result by reading the order.
    names = ["ZOHO_LOCK_CLIENT_ID", "ZOHO_LOCK_CLIENT_SECRET", "ZOHO_LOCK_REFRESH_TOKEN", "ZOHO_SO_LOCK_API_BASE", "ZOHO_ORG_ID"]
    names.append("ZOHO_SO_LOCK_WEBHOOK_URL" if mode() == "webhook" else "ZOHO_SO_LOCK_CONFIGURATION_ID")
    return all(os.environ.get(name, "").strip() for name in names)


def health_status() -> str:
    return "ok" if configured() else "not_configured"


def log_configuration_warning() -> None:
    if not configured():
        logger.warning("[ZOHO_SO_LOCK] so_lock: not_configured")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ZohoError("not_configured")
    return value


def _base_url() -> str:
    return _required("ZOHO_SO_LOCK_API_BASE").rstrip("/")


def reset_token_cache() -> None:
    global _access_token, _access_token_expires_at
    with _token_lock:
        _access_token = None
        _access_token_expires_at = 0.0


def get_access_token() -> str:
    if _access_token and time.time() < _access_token_expires_at - 60:
        return _access_token
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
                "client_id": _required("ZOHO_LOCK_CLIENT_ID"),
                "client_secret": _required("ZOHO_LOCK_CLIENT_SECRET"),
                "refresh_token": _required("ZOHO_LOCK_REFRESH_TOKEN"),
            },
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise ZohoError("Zoho lock connection is unavailable.") from exc
    if response.status_code != 200:
        raise ZohoError("Zoho lock connection needs re-authentication.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ZohoError("Zoho lock connection needs re-authentication.") from exc
    token = payload.get("access_token")
    if not token:
        raise ZohoError("Zoho lock connection needs re-authentication.")
    _access_token = token
    _access_token_expires_at = time.time() + int(payload.get("expires_in", 3600))
    logger.info("[ZOHO_SO_LOCK] token refresh status=ok expires_in=%ss", int(payload.get("expires_in", 3600)))
    return token


def lock_status_from_record(record: dict | None) -> dict:
    record = record or {}
    details = record.get("lock_details")
    if not isinstance(details, dict):
        lock_detail = record.get("lock_detail") if isinstance(record.get("lock_detail"), dict) else {}
        custom_locks = lock_detail.get("custom_locks") if isinstance(lock_detail, dict) else []
        if isinstance(custom_locks, list):
            for item in custom_locks:
                nested = item.get("lock_details") if isinstance(item, dict) else None
                if isinstance(nested, dict):
                    details = nested
                    break
    details = details if isinstance(details, dict) else {}
    return {
        "is_locked": bool(details.get("is_locked")),
        "config_id": str(details.get("locking_config_id") or ""),
        "config_name": details.get("locking_config_name") or None,
        "locked_by": details.get("locked_by") or None,
        "lock_time": details.get("lock_time") or None,
        "reason": details.get("lock_reason") or None,
    }


def _request(method: str, url: str, *, params: dict | None = None, data: dict | None = None) -> httpx.Response:
    org_id = _required("ZOHO_ORG_ID")
    limiter = zoho_rate_limiter.limiter_for(org_id)
    token = get_access_token()  # Accounts endpoint: not gated as Inventory (same as the main client)
    with limiter.admit():
        zoho_usage.record_call("lock_status")
        return httpx.request(
            method,
            url,
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params=params,
            data=data,
            timeout=REQUEST_TIMEOUT,
        )


def _scope_error(status_code: int, payload: dict) -> bool:
    message = str(payload.get("message") or payload.get("error") or "").lower()
    code = str(payload.get("code") or "")
    return status_code in {401, 403} or code == "57" or "not authorized" in message or "scope" in message


def _payload(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _already_locked(payload: dict) -> bool:
    text = " ".join(str(payload.get(key) or "") for key in ("message", "error", "details")).lower()
    return "already" in text and "lock" in text


_SO_ID = re.compile(r"^\d{1,32}$")

# Zoho rejects a lock reason of 100+ characters ("reason has less than 100 characters"); stay well under it.
REASON_BASE = "Acknowledged by Supply Chain Department via IntelliFleet"
REASON_MAX_LENGTH = 90


def _encoded_length(text: str) -> int:
    """Length of the reason as it travels inside JSONString (quotes and backslashes in a name grow when escaped)."""
    return len(json.dumps(text, ensure_ascii=False)) - 2  # minus the surrounding quotes


def build_reason(user: str | None) -> str:
    """Base text, plus " - {user}" only while it fits in REASON_MAX_LENGTH; a long name is truncated.
    No date/time: Zoho records lock_time and locked_by itself."""
    actor = " ".join(str(user or "").split())
    if not actor:
        return REASON_BASE
    name = actor
    while name and _encoded_length(f"{REASON_BASE} - {name}") > REASON_MAX_LENGTH:
        name = name[:-1]
    name = name.rstrip()
    return f"{REASON_BASE} - {name}" if name else REASON_BASE


def _post_details(response: httpx.Response, payload: dict) -> dict:
    return {"lock_http_status": response.status_code, "lock_zoho_code": payload.get("code")}


def _empty_status(error: str, message: str | None = None) -> dict:
    status = {"is_locked": False, "config_id": "", "config_name": None, "locked_by": None, "lock_time": None, "reason": None, "lock_error": error}
    if message:
        status["message"] = message
    return status


def _connection_error(exc: Exception) -> str:
    """Token refresh / network failures become a lock_error; they must never escape to the caller."""
    text = str(exc)
    if text == "not_configured":
        return "not_configured"
    if "re-authentication" in text:
        return "lock_credential_needs_reauthentication"
    return "lock_connection_error"


def _safe_request(method: str, url: str, *, params: dict | None = None, data: dict | None = None) -> tuple[httpx.Response | None, str | None]:
    try:
        return _request(method, url, params=params, data=data), None
    except (ZohoError, httpx.HTTPError) as exc:
        logger.error("[ZOHO_SO_LOCK] %s request failed: %s", method, type(exc).__name__)
        return None, _connection_error(exc)


def get_lock_status(so_id: str) -> dict:
    """Lock state of ONE sales order via the lock credential. Never raises."""
    if not configured():
        return _empty_status("not_configured")
    so_id = str(so_id or "").strip()
    if not _SO_ID.match(so_id):
        return _empty_status("invalid_salesorder_id")
    response, error = _safe_request(
        "GET",
        f"{_base_url()}/salesorders/{so_id}",
        params={"organization_id": _required("ZOHO_ORG_ID")},
    )
    if error:
        return _empty_status(error)
    payload = _payload(response)
    if _scope_error(response.status_code, payload):
        return _empty_status("lock_credential_lacks_scope", payload.get("message"))
    if response.status_code < 200 or response.status_code >= 300:
        return _empty_status(payload.get("message") or f"HTTP {response.status_code}")
    return lock_status_from_record(payload.get("salesorder") if isinstance(payload.get("salesorder"), dict) else payload)


# Webhook mode: after the POST, read the order back up to 3 times, 1.5s apart, until lock_details says locked.
WEBHOOK_VERIFY_ATTEMPTS = 3
WEBHOOK_VERIFY_DELAY_S = 1.5


def _lock_via_webhook(so_id: str, status: dict) -> dict:
    """POST {"salesorder_id": id} to the incoming webhook (the API key lives in the URL, which is never logged),
    then verify through lock_details. Never raises."""
    try:
        url = _required("ZOHO_SO_LOCK_WEBHOOK_URL")
        limiter = zoho_rate_limiter.limiter_for(_required("ZOHO_ORG_ID"))
        with limiter.admit():
            zoho_usage.record_call("lock_status")
            response = httpx.request("POST", url, json={"salesorder_id": so_id}, timeout=REQUEST_TIMEOUT)
    except ZohoError as exc:
        return {"locked": False, "already_locked": False, "lock_status": status, "lock_error": _connection_error(exc)}
    except httpx.HTTPError as exc:
        # Log only the exception type: httpx messages can contain the request URL, and the URL carries the key.
        logger.error("[ZOHO_SO_LOCK] webhook request failed: %s", type(exc).__name__)
        return {"locked": False, "already_locked": False, "lock_status": status, "lock_error": "lock_connection_error"}
    payload = _payload(response)
    details = _post_details(response, payload)
    message = payload.get("message") if isinstance(payload.get("message"), str) else None
    logger.info("[ZOHO_SO_LOCK] webhook status=%s", response.status_code)
    code_ok = "code" not in payload or str(payload.get("code")) in {"0", "success"}
    if not (200 <= response.status_code < 300 and code_ok):
        return {"locked": False, "already_locked": False, "lock_status": status, "lock_error": message or f"HTTP {response.status_code}", "message": message, **details}
    verified: dict = status
    for attempt in range(WEBHOOK_VERIFY_ATTEMPTS):
        time.sleep(WEBHOOK_VERIFY_DELAY_S)
        verified = get_lock_status(so_id)
        if verified.get("is_locked"):
            return {"locked": True, "already_locked": False, "lock_status": verified, "lock_error": None, "message": message, **details}
    return {"locked": False, "already_locked": False, "lock_status": verified, "lock_error": verified.get("lock_error") or "lock_verify_failed", "message": message, **details}


def lock_salesorder(so_id: str, user: str | None = None) -> dict:
    """Lock exactly ONE sales order. Never raises: every failure is returned as locked=False + lock_error."""
    if not configured():
        return {"locked": False, "already_locked": False, "lock_status": None, "lock_error": "not_configured"}
    so_id = str(so_id or "").strip()
    if not _SO_ID.match(so_id):  # digits only: a crafted id must never add a second entity id
        return {"locked": False, "already_locked": False, "lock_status": None, "lock_error": "invalid_salesorder_id"}
    status = get_lock_status(so_id)
    if status.get("lock_error"):
        return {"locked": False, "already_locked": False, "lock_status": status, "lock_error": status["lock_error"]}
    if status.get("is_locked"):
        return {"locked": True, "already_locked": True, "lock_status": status, "lock_error": None}
    if mode() == "webhook":
        return _lock_via_webhook(so_id, status)
    config_id = _required("ZOHO_SO_LOCK_CONFIGURATION_ID")
    reason = build_reason(user)
    url = f"{_base_url()}/lock/{config_id}"
    response, error = _safe_request(
        "POST",
        f"{url}?entity=salesorder&entity_ids={so_id}&organization_id={_required('ZOHO_ORG_ID')}",
        data={"JSONString": json.dumps({"reason": reason}, separators=(",", ":"), ensure_ascii=False)},
    )
    if error:
        return {"locked": False, "already_locked": False, "lock_status": status, "lock_error": error}
    payload = _payload(response)
    details = _post_details(response, payload)
    if _scope_error(response.status_code, payload):
        return {"locked": False, "already_locked": False, "lock_status": status, "lock_error": "lock_credential_lacks_scope", "message": payload.get("message"), **details}
    post_ok = 200 <= response.status_code < 300 and str(payload.get("code", 0)) == "0"
    already = _already_locked(payload)
    if not post_ok and not already:
        return {"locked": False, "already_locked": False, "lock_status": status, "lock_error": payload.get("message") or f"HTTP {response.status_code}", "zoho_code": payload.get("code"), **details}
    verified = get_lock_status(so_id)
    if verified.get("is_locked") and str(verified.get("config_id") or "") == config_id:
        return {"locked": True, "already_locked": bool(already), "lock_status": verified, "lock_error": None, **details}
    return {"locked": False, "already_locked": bool(already), "lock_status": verified, "lock_error": verified.get("lock_error") or "lock_verify_failed", "zoho_code": payload.get("code"), "message": payload.get("message"), **details}
