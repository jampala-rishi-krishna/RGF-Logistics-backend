from __future__ import annotations

import logging
import os
import json
import threading
import time

import httpx

logger = logging.getLogger("staff_directory_cache")

STAFF_DIRECTORY_FEED_URL = "https://rareglobalfood.app.n8n.cloud/webhook/logistics-staff-directory"
# n8n "[LOGISTICS] Create Staff Record" (0bwm8sy0c6K24WJZ) - the only write path into the
# Logistics Staff Directory DataTable. Used by the "+ New Driver" form during assignment.
STAFF_CREATE_URL = "https://rareglobalfood.app.n8n.cloud/webhook/logistics-staff-create"


class StaffCreateError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code

_lock = threading.Lock()
_staff: list[dict] = []
_notify: list[dict] = []
_last_refreshed: float = 0.0
_refreshing = False

N8N_API_BASE = "https://rareglobalfood.app.n8n.cloud/api/v1"
STAFF_TABLE_ID = "akyMRuol1VpZXh1D"   # Logistics Staff Directory
NOTIFY_TABLE_ID = "b08snnQoGMH9zLp4"  # Logistics Team Notify List (Jed/Pau/Jomel)
# Only applies to the REST source: reading DataTables through n8n's public API does not
# create workflow executions, so a TTL is free. The webhook fallback stays startup + manual.
REST_TTL_SECONDS = 6 * 60 * 60
WEBHOOK_REFRESH_SECONDS = 6 * 60 * 60
LOCAL_CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache", "staff_directory.json")
SNAPSHOT_FILE = os.environ.get("STAFF_DIRECTORY_SNAPSHOT_PATH", "").strip() or LOCAL_CACHE_FILE
_n8n_calls = []


def _n8n_api_key() -> str:
    return os.environ.get("N8N_API_KEY", "").strip()


def _to_staff(row: dict) -> dict:
    """Same mapping as the '[LOGISTICS] Get Staff Directory' Format Staff Feed node."""
    return {
        "id": row.get("id"),
        "name": row.get("staffName") or row.get("name"),
        "title": row.get("title"),
        "warehouse": row.get("warehouseAssignment") or row.get("warehouse"),
        "email": row.get("email"),
        "phone": row.get("contactNumber") or row.get("phone"),
        "role": row.get("role"),
        "active": True,
    }


def _rest_rows(table_id: str) -> list[dict]:
    rows: list[dict] = []
    cursor = None
    with httpx.Client(base_url=N8N_API_BASE, headers={"X-N8N-API-KEY": _n8n_api_key()}, timeout=20) as client:
        while True:
            params = {"limit": 250, **({"cursor": cursor} if cursor else {})}
            response = client.get(f"/data-tables/{table_id}/rows", params=params)
            response.raise_for_status()
            body = response.json()
            rows.extend(body.get("data") or [])
            cursor = body.get("nextCursor")
            if not cursor:
                return rows


def _webhook_staff() -> list[dict]:
    # Reuses the existing INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET / "Header Auth account"
    # n8n credential rather than a dedicated secret.
    secret = os.environ.get("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET", "").strip()
    headers = {"Authorization": secret} if secret else {}
    _record_n8n_call("staff-directory")
    response = httpx.get(STAFF_DIRECTORY_FEED_URL, headers=headers, timeout=20)
    if "execution limit reached" in response.text.lower():
        logger.error("[StaffDirectoryCache] n8n execution limit reached")
    response.raise_for_status()
    return response.json().get("staff") or []

def _record_n8n_call(name: str) -> None:
    now = time.time()
    _n8n_calls.append(now)
    _n8n_calls[:] = [stamp for stamp in _n8n_calls if now - stamp < 3600]
    logger.info("[n8n] call=%s calls_last_hour=%d", name, len(_n8n_calls))

def _load_disk() -> None:
    global _last_refreshed
    try:
        # A fresh writable local cache wins; Render's read-only Secret File is fallback.
        candidates = [LOCAL_CACHE_FILE] + ([SNAPSHOT_FILE] if SNAPSHOT_FILE != LOCAL_CACHE_FILE else [])
        snapshot_path = next((path for path in candidates if os.path.exists(path)), None)
        if not snapshot_path:
            raise FileNotFoundError(LOCAL_CACHE_FILE)
        with open(snapshot_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        staff = rows if isinstance(rows, list) else (payload.get("staff") or [])
        with _lock:
            _staff[:] = [_to_staff(row) for row in staff]
            _notify[:] = [_to_staff(row) for row in (payload.get("notify") or [])]
            _last_refreshed = float(payload.get("refreshed_at") or 0)
        logger.warning("[StaffDirectoryCache] Loaded %d staff from disk path=%s", len(_staff), snapshot_path)
    except (FileNotFoundError, ValueError, OSError):
        logger.info("[StaffDirectoryCache] No disk fallback available")

def _save_disk(staff: list[dict], notify: list[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(LOCAL_CACHE_FILE), exist_ok=True)
        temporary = f"{LOCAL_CACHE_FILE}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"staff": staff, "notify": notify, "refreshed_at": time.time()}, handle)
        os.replace(temporary, LOCAL_CACHE_FILE)
    except OSError:
        logger.exception("[StaffDirectoryCache] Could not persist disk fallback")


def source() -> str:
    return "n8n-rest" if _n8n_api_key() else "n8n-webhook"


def refresh() -> None:
    """Replace the staff (and, with the REST source, notify list) cache from n8n.

    - N8N_API_KEY set: n8n public REST API (no executions), refreshed at startup, on demand,
      and lazily every 15 min.
    - Otherwise: the '[LOGISTICS] Get Staff Directory' webhook, at startup and on demand only
      (POST /api/dispatch/staff/refresh, POST /api/admin/refresh-staff) - each fetch is a
      billed n8n execution, so no timer. The Notify List has no webhook; it stays empty."""
    global _last_refreshed, _refreshing
    try:
        if _n8n_api_key():
            staff = [_to_staff(r) for r in _rest_rows(STAFF_TABLE_ID)]
            notify = [_to_staff(r) for r in _rest_rows(NOTIFY_TABLE_ID)]
        else:
            staff, notify = _webhook_staff(), None
        with _lock:
            _staff[:] = staff
            if notify is not None:
                _notify[:] = notify
            _last_refreshed = time.time()
            saved_staff, saved_notify = list(_staff), list(_notify)
        _save_disk(saved_staff, saved_notify)
        logger.info("[StaffDirectoryCache] Refreshed via %s: %d staff, %d notify", source(), len(staff), len(_notify))
    except Exception:
        logger.exception("[StaffDirectoryCache] Refresh failed - keeping previous cache (%d entries)", len(_staff))
    finally:
        _refreshing = False


def _refresh_if_stale() -> None:
    """REST source only: kick a background refresh once the TTL has passed; never blocks."""
    global _refreshing
    if not _n8n_api_key() or time.time() - _last_refreshed < REST_TTL_SECONDS:
        return
    with _lock:
        if _refreshing:
            return
        _refreshing = True
    threading.Thread(target=refresh, name="staff-directory-refresh", daemon=True).start()


def notify_list() -> list[dict]:
    _refresh_if_stale()
    with _lock:
        return list(_notify)


def create(*, name: str, email: str, phone: str, title: str | None = None, warehouse: str | None = None, role: str | None = None) -> dict:
    """Insert one row into the n8n Staff Directory and add it to the cache immediately, so
    the new person is assignable right away without waiting for the next refresh()."""
    secret = os.environ.get("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET", "").strip()
    headers = {"Authorization": secret} if secret else {}
    payload = {
        "staffName": name,
        "email": email,
        "contactNumber": phone,
        "title": title or "DELIVERY DRIVER",
        "warehouseAssignment": warehouse or "",
        "role": role or "DRIVER",
    }
    try:
        response = httpx.post(STAFF_CREATE_URL, headers=headers, json=payload, timeout=20)
    except httpx.HTTPError as exc:
        raise StaffCreateError(f"Could not reach the n8n Staff Directory: {exc}") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code == 400:
        raise StaffCreateError(body.get("error") or "Staff Directory rejected the driver details.", 400)
    if response.status_code >= 300 or not isinstance(body.get("staff"), dict) or body["staff"].get("id") is None:
        raise StaffCreateError(body.get("error") or body.get("message") or f"Staff Directory create failed (HTTP {response.status_code}).")
    staff = body["staff"]
    with _lock:
        _staff[:] = [s for s in _staff if s.get("id") != staff["id"]]
        _staff.append(staff)
    logger.info("[StaffDirectoryCache] Created staff id=%s name=%s", staff["id"], staff.get("name"))
    return staff


def find_by_contact(email: str | None, phone_digits: str | None) -> dict | None:
    """Existing record with the same email or phone (last 10 digits), to avoid duplicate rows."""
    email = (email or "").strip().lower()
    with _lock:
        for s in _staff:
            if email and str(s.get("email") or "").strip().lower() == email:
                return s
            existing_digits = "".join(ch for ch in str(s.get("phone") or "") if ch.isdigit())
            if phone_digits and existing_digits and existing_digits[-10:] == phone_digits[-10:]:
                return s
    return None


def all_staff() -> list[dict]:
    _refresh_if_stale()
    with _lock:
        return list(_staff)


def get_by_id(staff_id: int, *, retry_on_miss: bool = True) -> dict | None:
    """A miss usually just means someone was added in n8n after our last refresh - refresh
    once and retry before giving up, instead of waiting for the next manual refresh."""
    with _lock:
        found = next((s for s in _staff if s.get("id") == staff_id), None)
    # Never trigger a billed webhook from a per-request cache miss. Manual refresh
    # is the only webhook refresh path when the REST API key is unavailable.
    return found

_load_disk()


def first_active() -> dict | None:
    with _lock:
        return next((s for s in _staff if s.get("active")), None)


def last_refreshed() -> float:
    with _lock:
        return _last_refreshed
