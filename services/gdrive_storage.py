"""Google Drive (API v3) client for trip photos. Contract: docs/GDRIVE_MEDIA.md.

The photo folder lives in the My Drive of its owner (martin@): a service account cannot own files there, so the backend
uploads WITH OAUTH AS THE FOLDER OWNER. Env (read on every call, never logged or returned):
  GDRIVE_PHOTO_FOLDER_ID   root folder id (never hardcoded)
  GDRIVE_REFRESH_TOKEN     refresh token of the folder owner, scope https://www.googleapis.com/auth/drive (NOT the Gmail token)
  GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET   optional; fall back to GMAIL_COMMS_CLIENT_ID / GMAIL_COMMS_CLIENT_SECRET (same OAuth client)
The access token is cached in memory until shortly before it expires. Plain httpx (no Google SDK); tests install `_transport`.

Errors: an expired / revoked token -> DriveAuthError (logged as "Drive token expired/revoked", never the token). 403 rate limit,
429 and 5xx are retried ONCE after a short backoff, then DriveError. Callers answer 503 so the phone keeps the photo queued.
No per-file "anyone with link" permission is ever created: viewing goes through GET /api/media/{id}, which streams the bytes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid

import httpx

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
FILE_FIELDS = "id,name,description,md5Checksum,parents,appProperties"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "backendError"})
BACKOFF_SECONDS = 1.5
MAX_NAME_SUFFIX = 50

_transport: httpx.BaseTransport | None = None  # tests install an httpx.MockTransport here; None = real network
_sleep = time.sleep  # tests replace it so the backoff does not slow them down
_token: dict = {"value": None, "expires_at": 0.0}
_token_lock = threading.Lock()
_folders: dict[tuple[str, str], str] = {}  # (root id, "YYYY-MM-DD") -> folder id
_folder_lock = threading.Lock()
_name_locks: dict[str, threading.Lock] = {}
_name_locks_guard = threading.Lock()


class DriveError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class DriveAuthError(DriveError):
    """The refresh token is missing, expired or revoked: someone has to re-run scripts/gdrive_refresh_token.py."""


class DriveNotFound(DriveError):
    pass


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def client_id() -> str:
    return _env("GDRIVE_CLIENT_ID") or _env("GMAIL_COMMS_CLIENT_ID")


def client_secret() -> str:
    return _env("GDRIVE_CLIENT_SECRET") or _env("GMAIL_COMMS_CLIENT_SECRET")


def root_folder_id() -> str:
    return _env("GDRIVE_PHOTO_FOLDER_ID")


def configured() -> bool:
    """True when the folder id, the Drive refresh token and an OAuth client id / secret are all set."""
    return bool(root_folder_id() and _env("GDRIVE_REFRESH_TOKEN") and client_id() and client_secret())


def reset_cache() -> None:
    """Forget the access token and the day-folder ids (tests; or after the env changed)."""
    with _token_lock:
        _token.update(value=None, expires_at=0.0)
    with _folder_lock:
        _folders.clear()


def _http() -> httpx.Client:
    return httpx.Client(transport=_transport, timeout=httpx.Timeout(30.0, connect=10.0))


def _error_reason(response: httpx.Response) -> str:
    """Google's short error reason (never the request headers / token)."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        reasons = ",".join(str(e.get("reason")) for e in error.get("errors") or [] if isinstance(e, dict) and e.get("reason"))
        return reasons or str(error.get("status") or error.get("message") or f"HTTP {response.status_code}")
    return str(error or f"HTTP {response.status_code}")


def _access_token(force: bool = False) -> str:
    with _token_lock:
        if not force and _token["value"] and time.time() < _token["expires_at"] - 60:
            return _token["value"]
        if not configured():
            raise DriveAuthError("Google Drive is not configured on the server.", 503)
        try:
            with _http() as client:
                response = client.post(TOKEN_URL, data={"client_id": client_id(), "client_secret": client_secret(), "refresh_token": _env("GDRIVE_REFRESH_TOKEN"), "grant_type": "refresh_token"})
        except httpx.HTTPError as exc:
            raise DriveError(f"Could not reach Google to authorize Drive: {type(exc).__name__}", 503) from exc
        if response.status_code != 200:
            reason = _error_reason(response)
            _token.update(value=None, expires_at=0.0)
            if response.status_code in (400, 401) or reason == "invalid_grant":
                logger.error("[GDRIVE] Drive token expired/revoked (token refresh answered %s %s): re-run scripts/gdrive_refresh_token.py", response.status_code, reason)
                raise DriveAuthError("Drive token expired/revoked.", 503)
            raise DriveError(f"Drive token refresh failed ({reason}).", 503)
        payload = response.json()
        _token.update(value=payload["access_token"], expires_at=time.time() + int(payload.get("expires_in", 3600)))
        return _token["value"]


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code in RETRY_STATUSES:
        return True
    return response.status_code == 403 and any(reason in RATE_REASONS for reason in _error_reason(response).split(","))


def _request(method: str, url: str, *, headers: dict | None = None, retry: bool = True, **kwargs) -> httpx.Response:
    """One Drive call: bearer auth (one forced token refresh on 401), one backoff retry on rate limit / 5xx / network error
    (`retry=False` for a create, whose retry the caller makes duplicate-safe itself by looking the upload_key up first)."""
    forced = False
    retried = not retry
    while True:
        sent = {**(headers or {}), "Authorization": f"Bearer {_access_token(force=forced)}"}
        try:
            with _http() as client:
                response = client.request(method, url, headers=sent, **kwargs)
        except httpx.HTTPError as exc:
            if not retried:
                retried = True
                _sleep(BACKOFF_SECONDS)
                continue
            raise DriveError(f"Drive request failed: {type(exc).__name__}", 503) from exc
        if response.status_code == 401:
            if not forced:
                forced = True
                continue
            logger.error("[GDRIVE] Drive token expired/revoked (API answered 401)")
            with _token_lock:
                _token.update(value=None, expires_at=0.0)
            raise DriveAuthError("Drive token expired/revoked.", 503)
        if _is_rate_limited(response):
            if not retried:
                retried = True
                _sleep(BACKOFF_SECONDS)
                continue
            logger.warning("[GDRIVE] %s %s still failing after one retry: %s %s", method, url.split("?")[0], response.status_code, _error_reason(response))
            raise DriveError(f"Drive is busy ({response.status_code}).", 503)
        if response.status_code == 404:
            raise DriveNotFound("Drive file not found.", 404)
        if response.status_code >= 300:
            logger.warning("[GDRIVE] %s %s failed: %s %s", method, url.split("?")[0], response.status_code, _error_reason(response))
            raise DriveError(f"Drive request failed ({response.status_code}).", 503)
        return response


def q_literal(value: str) -> str:
    """A value quoted for a Drive `q` expression."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _list(q: str, fields: str = f"files({FILE_FIELDS})", page_size: int = 100) -> list[dict]:
    response = _request("GET", f"{API}/files", params={"q": q, "fields": fields, "pageSize": str(page_size), "supportsAllDrives": "true", "includeItemsFromAllDrives": "true", "spaces": "drive"})
    return list(response.json().get("files") or [])


# ---- folders ------------------------------------------------------------------------------------------------------


def day_folder(day: str) -> str:
    """Id of the `YYYY-MM-DD` subfolder of the root folder; created on first use. Looked up by name + parent first (so a folder
    created by another instance / an earlier run is reused) and under a lock (so two uploads of the same day create one folder)."""
    root = root_folder_id()
    key = (root, day)
    found = _folders.get(key)
    if found:
        return found
    with _folder_lock:
        found = _folders.get(key)
        if found:
            return found
        existing = _list(f"name = {q_literal(day)} and {q_literal(root)} in parents and mimeType = {q_literal(FOLDER_MIME)} and trashed = false", "files(id,name,createdTime)")
        if existing:
            existing.sort(key=lambda f: (f.get("createdTime") or "", f["id"]))  # two folders of one day (an old race): always the oldest
            folder_id = existing[0]["id"]
        else:
            response = _request("POST", f"{API}/files", params={"supportsAllDrives": "true", "fields": "id"}, json={"name": day, "mimeType": FOLDER_MIME, "parents": [root]})
            folder_id = response.json()["id"]
        _folders[key] = folder_id
        return folder_id


# ---- files --------------------------------------------------------------------------------------------------------


_SUFFIX = re.compile(r"^(?P<base>.*?)(?:_(?P<n>\d+))?\.jpg$", re.I)


def split_name(name: str) -> tuple[str, int]:
    """'SO1_2026-10-15_13-42_delivered_2.jpg' -> ('SO1_2026-10-15_13-42_delivered', 2); no suffix -> 1."""
    match = _SUFFIX.match(name or "")
    if not match:
        return name or "", 1
    return match.group("base"), int(match.group("n") or 1)


def _candidate(base: str, n: int) -> str:
    return f"{base}.jpg" if n <= 1 else f"{base}_{n}.jpg"


def _name_lock(folder_id: str, base: str) -> threading.Lock:
    with _name_locks_guard:
        if len(_name_locks) > 2000:
            _name_locks.clear()
        return _name_locks.setdefault(f"{folder_id}/{base}", threading.Lock())


def free_name(folder_id: str, base: str, *, exclude_id: str | None = None) -> str:
    """`base.jpg`, else the first free `base_2.jpg`, `base_3.jpg`, ... in that folder."""
    names = [_candidate(base, n) for n in range(1, MAX_NAME_SUFFIX + 1)]
    taken: set[str] = set()
    for start in range(0, len(names), 25):  # keep each q expression short
        chunk = names[start:start + 25]
        clause = " or ".join(f"name = {q_literal(n)}" for n in chunk)
        taken |= {f["name"] for f in _list(f"{q_literal(folder_id)} in parents and trashed = false and ({clause})", "files(id,name)") if f.get("id") != exclude_id}
        free = next((n for n in chunk if n not in taken), None)
        if free:
            return free
    return f"{base}_{uuid.uuid4().hex[:6]}.jpg"


def find_by_app_property(key: str, value: str) -> dict | None:
    """The (non-trashed) file carrying appProperties[key] == value, e.g. a previous upload that crashed before its DB row."""
    files = _list(f"appProperties has {{ key={q_literal(key)} and value={q_literal(value)} }} and trashed = false")
    return files[0] if files else None


def upload(*, folder_id: str, base: str, data: bytes, description: str, app_properties: dict[str, str], mime_type: str = "image/jpeg") -> dict:
    """files.create (multipart) under `folder_id` with the first free name for `base`. Returns {id, name, md5Checksum, ...}.
    Not retried here: a create that timed out may still have made the file, so the caller looks it up by upload_key first."""
    with _name_lock(folder_id, base):  # two photos of the same SO in the same minute get _2, never the same name
        name = free_name(folder_id, base)
        metadata = {"name": name, "parents": [folder_id], "description": description, "appProperties": app_properties, "mimeType": mime_type}
        boundary = f"intellifleet-{uuid.uuid4().hex}"
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n").encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("ascii")
        response = _request("POST", f"{UPLOAD_API}/files", params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": FILE_FIELDS},
                            content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"}, retry=False)
        return response.json()


def get_metadata(file_id: str) -> dict:
    return _request("GET", f"{API}/files/{file_id}", params={"supportsAllDrives": "true", "fields": FILE_FIELDS}).json()


def update_metadata(file_id: str, *, name: str | None = None, description: str | None = None) -> dict:
    """files.update of name and / or description (content untouched)."""
    body = {k: v for k, v in (("name", name), ("description", description)) if v is not None}
    return _request("PATCH", f"{API}/files/{file_id}", params={"supportsAllDrives": "true", "fields": FILE_FIELDS}, json=body).json()


def download(file_id: str) -> bytes:
    """files.get?alt=media: the stored bytes."""
    return _request("GET", f"{API}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"}).content
