"""Photo / signature storage behind one switch: Google Drive (default) or Cloudflare R2. Never Neon (Neon keeps the `trip_photos`
metadata row only). Contracts: docs/GDRIVE_MEDIA.md (Drive) and docs/TRIP_WORKFLOW.md section 9 (R2).

Provider (`provider()`), env `MEDIA_STORAGE = gdrive | r2 | none`:
  * gdrive  Google Drive via services/gdrive_storage.py (GDRIVE_PHOTO_FOLDER_ID + GDRIVE_REFRESH_TOKEN + the OAuth client).
  * r2      Cloudflare R2 (S3-compatible, SigV4 below) - the previous store, kept behind the flag.
  * none    photo capture off.
  Unset: gdrive when its env is complete; else r2 when every R2_* variable is set (a deployment that only has R2 keeps working);
  else none. An explicit value whose env is incomplete means none (never a silent switch to the other store).
`storage_available()` (alias `is_enabled()`, used by routers/driver.py and services/trip_workflow.py) drives "photo not configured".

`trip_photos.storage_key`: `gdrive:{fileId}` for Drive; R2 keys are plain object keys (`trips/<batch>/<uuid>.jpg`, `r2:<key>` also
accepted). Old R2 rows keep resolving through R2 whenever the R2 env is set, whatever the current provider is.

Drive photos (re-encoded server-side, see services/image_processing.py) are named and described from data the server already
holds (batch rows, trip snapshot, trip stops, staff directory cache) - never a Zoho call, never the phone's word for the SO, customer,
truck or crew. The phone supplies only the capture time. After a check-out / issue report commits, the photo's Drive name and
description are updated with the final result (best effort, off the request; a failed update is retried on the next media read).

AWS Signature V4 (R2) is implemented with hmac / hashlib and sent with httpx (no boto3). Path-style addressing:
https://<account>.r2.cloudflarestorage.com/<bucket>/<key>, region "auto", service "s3". Secrets are read from the environment
on every call and are never logged or returned.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from services import gdrive_storage, image_processing

logger = logging.getLogger(__name__)

REQUIRED_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
REGION = "auto"
SERVICE = "s3"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
PROVIDERS = ("gdrive", "r2", "none")
DRIVE_PREFIX = "gdrive:"
R2_PREFIX = "r2:"
MANILA = ZoneInfo("Asia/Manila")
ISSUE_BATCH = "issue"  # trip_photos.batch_id of an issue photo that is not tied to a trip (routers/media.py)


class StorageError(Exception):
    pass


# ---- provider -------------------------------------------------------------------------------------------------------


def r2_configured() -> bool:
    """True only when every R2 variable is set."""
    return all(os.environ.get(name, "").strip() for name in REQUIRED_ENV)


def provider() -> str:
    """'gdrive' | 'r2' | 'none' (module docstring)."""
    wanted = os.environ.get("MEDIA_STORAGE", "").strip().lower()
    if wanted == "gdrive":
        return "gdrive" if gdrive_storage.configured() else "none"
    if wanted == "r2":
        return "r2" if r2_configured() else "none"
    if wanted == "none":
        return "none"
    if gdrive_storage.configured():
        return "gdrive"
    return "r2" if r2_configured() else "none"


def storage_available() -> bool:
    """Can a new photo be stored right now? False -> upload 503 `storage_disabled`, the app shows "photo not configured"."""
    return provider() != "none"


def is_enabled() -> bool:
    """Alias of storage_available() (routers/driver.py, services/trip_workflow.py and tests call it by this name)."""
    return storage_available()


def is_drive_key(storage_key: str | None) -> bool:
    return bool(storage_key) and storage_key.startswith(DRIVE_PREFIX)


def drive_file_id(storage_key: str) -> str:
    return storage_key[len(DRIVE_PREFIX):]


def r2_object_key(storage_key: str) -> str:
    return storage_key[len(R2_PREFIX):] if storage_key.startswith(R2_PREFIX) else storage_key


# ---- Drive photo naming / description ----------------------------------------------------------------------------------

_UNSAFE_NAME = re.compile(r'[/\\:*?"<>|\x00-\x1f]+')
RESULT_LABEL = {"delivered": "Delivered", "partial": "Partial", "failed": "Not delivered", "cancel_proof": "Cancelled by request"}
FINAL_KINDS = frozenset(RESULT_LABEL)


@dataclass
class PhotoMeta:
    """What a Drive photo's name / description / appProperties say. Everything is derived server-side."""

    batch_id: str
    so_id: str | None
    field_key: str
    kind: str  # delivery (placeholder until check-out) | delivered | partial | failed | problem | signature | photo
    order_type: str | None = None  # SO | WM | TO | PO
    order_number: str | None = None
    customer: str | None = None
    truck: str | None = None
    driver: str | None = None
    helpers: list[str] = field(default_factory=list)
    invoice_no: str | None = None
    issue_category: str | None = None
    issue_reference: str | None = None


def sanitize_name(value: str) -> str:
    """File-name safe: `/ \\ : * ? " < > |` and control characters become '-', whitespace is collapsed."""
    return re.sub(r"\s+", " ", _UNSAFE_NAME.sub("-", str(value or ""))).strip() or "photo"


def upload_kind(field_key: str | None) -> str:
    """Kind at upload time, from the form field: delivery photo -> 'delivery' (placeholder until check-out), failure photo ->
    'failed', issue photo -> 'problem', a signature -> 'signature', anything else (checklist photos) -> 'photo'."""
    key = (field_key or "").lower()
    if key == "issue_photo":
        return "problem"
    if key == "failure_photo":
        return "failed"
    if key == "cancel_proof":
        return "cancel_proof"
    if key == "delivery_photo":
        return "delivery"
    if "signature" in key:
        return "signature"
    return "photo"


def outcome_kind(outcome: str | None) -> str | None:
    """'delivered' | 'partial' | 'failed' | None for a stop outcome (template options: Delivered / Partially delivered /
    Failed / Returned)."""
    text = str(outcome or "").strip().lower()
    if not text:
        return None
    if text.startswith("cancelled"):
        return "cancel_proof"
    if text.startswith("partial"):
        return "partial"
    if text.startswith(("fail", "return", "not deliver", "undeliver")):
        return "failed"
    if text.startswith("deliver"):
        return "delivered"
    return None


def manila(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(MANILA)


def _order_prefix(meta: PhotoMeta) -> str:
    return {"TO": "TO", "PO": "PO"}.get((meta.order_type or "").upper(), "SO")


def file_base(meta: PhotoMeta, captured: datetime) -> str:
    """`{SO/TO number}_{YYYY-MM-DD}_{HH-MM}_{kind}` (Manila capture time); '.jpg' and any `_2` are added by gdrive_storage."""
    local = manila(captured)
    number = meta.order_number or ("ISSUE" if meta.kind == "problem" else "PHOTO")
    return sanitize_name(f"{number}_{local:%Y-%m-%d}_{local:%H-%M}_{meta.kind}")


def result_line(meta: PhotoMeta) -> str | None:
    if meta.kind == "problem":
        return f"Problem: {meta.issue_category}" if meta.issue_category else "Problem"
    return RESULT_LABEL.get(meta.kind)


def description(meta: PhotoMeta, captured: datetime) -> str:
    """The Drive `description`: plain text, one item per line; a line whose value is unknown is left out."""
    local = manila(captured)
    prefix = _order_prefix(meta)
    lines = []
    if meta.order_number:
        lines.append(f"{prefix}: {meta.order_number}")
    lines += [f"Date: {local:%Y-%m-%d}", f"Time: {local:%H:%M} (Manila)"]
    if meta.customer:
        lines.append(f"Customer: {meta.customer}")
    result = result_line(meta)
    if result:
        lines.append(f"Result: {result}")
    if meta.issue_reference:
        lines.append(f"Issue: {meta.issue_reference}")
    if prefix in ("TO", "PO"):
        if meta.order_number:
            lines.append(f"{prefix}#: {meta.order_number}")
    elif meta.invoice_no:
        lines.append(f"Invoice/DR: {meta.invoice_no}")
    if meta.truck:
        lines.append(f"Truck: {meta.truck}")
    crew = ([f"Driver: {meta.driver}"] if meta.driver else []) + ([f"Helper: {', '.join(meta.helpers)}"] if meta.helpers else [])
    if crew:
        lines.append("    ".join(crew))
    if meta.batch_id and meta.batch_id != ISSUE_BATCH:
        lines.append(f"Trip: {meta.batch_id}")
    return "\n".join(lines)


def app_properties(meta: PhotoMeta, upload_key: str, captured: datetime) -> dict[str, str]:
    """Drive appProperties (key + value <= 124 bytes each; empty values left out)."""
    values = {"upload_key": upload_key, "batch_id": meta.batch_id, "so_id": meta.so_id or "", "so_number": meta.order_number or "", "field_key": meta.field_key,
              "captured_at": captured.astimezone(timezone.utc).isoformat(timespec="seconds"), "app": "intellifleet"}
    return {k: str(v)[: 120 - len(k)] for k, v in values.items() if v not in (None, "")}


def photo_meta(db, *, batch_id: str, so_id: str | None, field_key: str, uploader_staff_id: int | None = None) -> PhotoMeta:
    """PhotoMeta at upload time from the rows the server already has (batch rows + trip snapshot + staff directory cache). No Zoho."""
    from services import order_types
    from services.trip_timeline import batch_crew, batch_rows, staff_name

    meta = PhotoMeta(batch_id=batch_id, so_id=so_id, field_key=field_key, kind=upload_kind(field_key))
    if batch_id == ISSUE_BATCH:
        meta.driver = staff_name(uploader_staff_id) if uploader_staff_id is not None else None
        return meta
    rows = batch_rows(db, batch_id)
    row = next((r for r in rows if r.id == so_id), None) if so_id else None
    if row is not None:
        meta.order_type = order_types.order_type_of(row)
        meta.order_number = row.salesorder_number or row.id
        meta.customer = row.customer_name
    crew = batch_crew(db, batch_id)
    if crew is not None:
        meta.truck = crew.get("vehicle") or (row.vehicle_id if row is not None else None)
        meta.driver = staff_name(crew.get("driver_id")) if crew.get("driver_id") is not None else None
        meta.helpers = [name for name in (staff_name(h) for h in crew.get("helper_ids") or []) if name]
    return meta


# ---- Drive photo store / read ------------------------------------------------------------------------------------------

FULL_CACHE = image_processing.ByteLRU(48 * 1024 * 1024)  # ~64 MB with the thumbs; per process (instance-local)
THUMB_CACHE = image_processing.ByteLRU(16 * 1024 * 1024)
_md5: dict[str, str] = {}
_pending: dict[str, PhotoMeta] = {}  # file id -> metadata whose Drive update failed; retried on the next media read
_pending_lock = threading.Lock()
MAX_PENDING = 5000


def _remember(file_id: str, data: bytes | None = None, md5: str | None = None) -> None:
    if len(_md5) > 20000:
        _md5.clear()
    if data is not None:
        FULL_CACHE.put(file_id, data)
        md5 = md5 or hashlib.md5(data).hexdigest()
    if md5:
        _md5[file_id] = md5


def reset_caches() -> None:
    FULL_CACHE.clear()
    THUMB_CACHE.clear()
    _md5.clear()
    with _pending_lock:
        _pending.clear()


def store_drive_photo(meta: PhotoMeta, upload_key: str, captured: datetime, jpeg: bytes) -> dict:
    """Upload once per upload_key: a file already in Drive with this upload_key (an earlier attempt that died before its DB
    row) is reused; a failed create is retried once after a backoff, after looking the key up again. Raises DriveError."""
    # Concurrent retries can both arrive before the metadata row commits. Protect
    # the lookup/create pair, not just the filename selection inside upload().
    with gdrive_storage._name_lock("upload-key", upload_key):
        return _store_drive_photo(meta, upload_key, captured, jpeg)


def _store_drive_photo(meta: PhotoMeta, upload_key: str, captured: datetime, jpeg: bytes) -> dict:
    existing = gdrive_storage.find_by_app_property("upload_key", upload_key)
    if existing is not None:
        logger.info("[MEDIA] reusing the Drive file of an earlier attempt of this upload_key (its row was missing)")
        _remember(existing["id"], md5=existing.get("md5Checksum"))
        return existing
    folder_id = gdrive_storage.day_folder(f"{manila(captured):%Y-%m-%d}")
    kwargs = dict(folder_id=folder_id, base=file_base(meta, captured), data=jpeg, description=description(meta, captured), app_properties=app_properties(meta, upload_key, captured))
    try:
        stored = gdrive_storage.upload(**kwargs)
    except gdrive_storage.DriveAuthError:
        raise
    except gdrive_storage.DriveError:
        gdrive_storage._sleep(gdrive_storage.BACKOFF_SECONDS * 2)
        existing = gdrive_storage.find_by_app_property("upload_key", upload_key)  # the create may have landed before the error
        if existing is not None:
            _remember(existing["id"], md5=existing.get("md5Checksum"))
            return existing
        stored = gdrive_storage.upload(**kwargs)
    _remember(stored["id"], jpeg, stored.get("md5Checksum"))
    return stored


def etag_for(file_id: str, md5: str, thumb: bool) -> str:
    return f'"{file_id}-{md5}{"-t" if thumb else ""}"'


def known_etag(file_id: str, thumb: bool, *, ask_drive: bool = False) -> str | None:
    """The ETag without downloading: from memory, or (ask_drive) one metadata call for md5Checksum."""
    md5 = _md5.get(file_id)
    if md5 is None and ask_drive:
        md5 = gdrive_storage.get_metadata(file_id).get("md5Checksum")
        if md5:
            _remember(file_id, md5=md5)
    return etag_for(file_id, md5, thumb) if md5 else None


def read_drive_photo(file_id: str, thumb: bool) -> tuple[bytes, str]:
    """(jpeg bytes, ETag) of the stored 1600px image or its 320px thumbnail, through the in-process LRU caches."""
    if thumb:
        cached = THUMB_CACHE.get(file_id)
        if cached is not None and file_id in _md5:
            return cached, etag_for(file_id, _md5[file_id], True)
    full = FULL_CACHE.get(file_id)
    if full is None:
        full = gdrive_storage.download(file_id)
        _remember(file_id, full, hashlib.md5(full).hexdigest())  # Drive's md5Checksum is the MD5 of the content
    md5 = _md5.get(file_id) or hashlib.md5(full).hexdigest()
    if not thumb:
        return full, etag_for(file_id, md5, False)
    small = image_processing.thumbnail_jpeg(full)
    THUMB_CACHE.put(file_id, small)
    return small, etag_for(file_id, md5, True)


# ---- best-effort Drive metadata update (after check-out / after an issue report) ---------------------------------------


def _spawn(fn) -> None:
    """Run off the request (tests replace this with a direct call)."""
    threading.Thread(target=fn, daemon=True, name="gdrive-update").start()


def _captured_of(current: dict, fallback: datetime | None) -> datetime:
    raw = (current.get("appProperties") or {}).get("captured_at")
    try:
        if raw:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return fallback or datetime.now(timezone.utc)


def update_drive_file(file_id: str, meta: PhotoMeta) -> bool:
    """Make the Drive name / description match `meta` (capture time from the file's own appProperties). True when changed."""
    current = gdrive_storage.get_metadata(file_id)
    captured = _captured_of(current, None)
    wanted_description = description(meta, captured)
    base = file_base(meta, captured)
    name = None
    if gdrive_storage.split_name(current.get("name") or "")[0] != base:
        parent = (current.get("parents") or [None])[0]
        name = gdrive_storage.free_name(parent, base, exclude_id=file_id) if parent else f"{base}.jpg"
    if name is None and wanted_description == (current.get("description") or ""):
        return False
    gdrive_storage.update_metadata(file_id, name=name, description=wanted_description)
    return True


def _run_updates(jobs: list[tuple[str, PhotoMeta]], what: str) -> None:
    for file_id, meta in jobs:
        try:
            update_drive_file(file_id, meta)
            with _pending_lock:
                _pending.pop(file_id, None)
        except Exception as exc:  # best effort: the check-out / issue is already committed
            logger.warning("[MEDIA] Drive %s update failed for one photo (retried on its next view): %s", what, type(exc).__name__)
            with _pending_lock:
                if len(_pending) >= MAX_PENDING:
                    _pending.pop(next(iter(_pending)))
                _pending[file_id] = meta


def retry_pending(file_id: str) -> None:
    """Called on a media read: if this photo's Drive update failed earlier, try it again (off the request)."""
    with _pending_lock:
        meta = _pending.pop(file_id, None)
    if meta is not None:
        _spawn(lambda: _run_updates([(file_id, meta)], "retry"))


def pending_count() -> int:
    return len(_pending)


def _photo_ids_of(answers) -> set[int]:
    ids = set()
    for answer in answers or []:
        if isinstance(answer, dict) and answer.get("type") in ("photo", "signature"):
            try:
                ids.add(int(answer.get("value")))
            except (TypeError, ValueError):
                pass
    return ids


def after_checkout(db, batch_id: str, so_id: str) -> None:
    """Called after a check-out COMMITTED: the Drive photos of this stop get their final kind (delivered / partial / failed) in
    the name and the Result + Invoice/DR lines in the description (files.update). Never raises; Drive runs off the request."""
    try:
        if not gdrive_storage.configured():
            return
        from sqlalchemy import or_, select

        from models.trip import Trip, TripPhoto, TripStop
        from services import trip_state

        stop = db.execute(select(TripStop).join(Trip, Trip.id == TripStop.trip_id).where(Trip.batch_id == batch_id, TripStop.so_id == so_id)).scalars().first()
        if stop is None:
            return
        answer_ids = _photo_ids_of(stop.form_answers)
        clause = TripPhoto.so_id == so_id if not answer_ids else or_(TripPhoto.so_id == so_id, TripPhoto.id.in_(answer_ids))
        photos = db.execute(select(TripPhoto).where(TripPhoto.batch_id == batch_id, clause, TripPhoto.storage_key.like(DRIVE_PREFIX + "%"),
                                                    TripPhoto.field_key != "issue_photo")).scalars().all()
        if not photos:
            return
        final = outcome_kind(stop.outcome) or "delivered"
        base = photo_meta(db, batch_id=batch_id, so_id=so_id, field_key="delivery_photo")
        base.order_number = base.order_number or stop.so_number
        base.customer = base.customer or stop.customer_name
        base.invoice_no = trip_state.invoice_no_of(stop)
        jobs = []
        for p in photos:
            kind = upload_kind(p.field_key)
            jobs.append((drive_file_id(p.storage_key), replace(base, field_key=p.field_key, helpers=list(base.helpers), kind=kind if kind == "signature" else final)))
        _spawn(lambda: _run_updates(jobs, "check-out"))
    except Exception as exc:
        logger.warning("[MEDIA] after_checkout skipped: %s", type(exc).__name__)


def after_issue(db, issue) -> None:
    """Called after an issue report COMMITTED: its Drive photos get kind=problem, the issue reference (IS-...) and the category
    in their description. Never raises; Drive runs off the request."""
    try:
        if not gdrive_storage.configured() or not issue.photo_ids:
            return
        from sqlalchemy import select

        from models.trip import TripPhoto

        photos = db.execute(select(TripPhoto).where(TripPhoto.id.in_([int(i) for i in issue.photo_ids]), TripPhoto.storage_key.like(DRIVE_PREFIX + "%"))).scalars().all()
        jobs = []
        for p in photos:
            meta = photo_meta(db, batch_id=p.batch_id, so_id=p.so_id or issue.so_id, field_key=p.field_key, uploader_staff_id=issue.reporter_staff_id)
            meta.kind = "problem"
            meta.issue_reference = issue.reference
            meta.issue_category = issue.category
            meta.order_number = meta.order_number or issue.so_number
            meta.truck = meta.truck or issue.vehicle
            jobs.append((drive_file_id(p.storage_key), meta))
        if jobs:
            _spawn(lambda: _run_updates(jobs, "issue"))
    except Exception as exc:
        logger.warning("[MEDIA] after_issue skipped: %s", type(exc).__name__)


# ---- R2 (S3 SigV4) ------------------------------------------------------------------------------------------------------




def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _endpoint() -> str:
    return (_env("R2_ENDPOINT") or f"https://{_env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com").rstrip("/")


# ---- SigV4 primitives (pure; exercised by the AWS published test vectors) ----------------------------------------


def uri_encode(value: str, *, keep_slash: bool = False) -> str:
    return quote(value, safe="-_.~" + ("/" if keep_slash else ""))


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    key = _hmac(("AWS4" + secret).encode("utf-8"), date_stamp)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    return key


def canonical_request(method: str, path: str, query: dict[str, str], headers: dict[str, str], payload_hash: str) -> tuple[str, str]:
    """(canonical request text, signed header names). `headers` are the headers to sign; names are lower-cased here."""
    lowered = {name.lower(): " ".join(str(value).split()) for name, value in headers.items()}
    names = sorted(lowered)
    canonical_query = "&".join(f"{uri_encode(k)}={uri_encode(v)}" for k, v in sorted(query.items()))
    canonical_headers = "".join(f"{name}:{lowered[name]}\n" for name in names)
    signed = ";".join(names)
    text = "\n".join([method.upper(), uri_encode(path, keep_slash=True), canonical_query, canonical_headers, signed, payload_hash])
    return text, signed


def signature_for(canonical: str, *, secret: str, amz_date: str, region: str, service: str) -> str:
    date_stamp = amz_date[:8]
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    return hmac.new(signing_key(secret, date_stamp, region, service), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _object_url_parts(key: str) -> tuple[str, str, str]:
    endpoint = _endpoint()
    host = endpoint.split("://", 1)[-1]
    return endpoint, host, f"/{_env('R2_BUCKET')}/{key.lstrip('/')}"


# ---- operations ---------------------------------------------------------------------------------------------------


def put_object(key: str, data: bytes, content_type: str, *, now: datetime | None = None) -> None:
    """Upload bytes to `key`. Raises StorageError when storage is not configured or the request fails."""
    if not r2_configured():
        raise StorageError("object storage is not configured")
    endpoint, host, path = _object_url_parts(key)
    amz_date = _stamp(now)
    payload_hash = hashlib.sha256(data).hexdigest()
    headers = {"host": host, "content-type": content_type, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    canonical, signed = canonical_request("PUT", path, {}, headers, payload_hash)
    signature = signature_for(canonical, secret=_env("R2_SECRET_ACCESS_KEY"), amz_date=amz_date, region=REGION, service=SERVICE)
    scope = f"{amz_date[:8]}/{REGION}/{SERVICE}/aws4_request"
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={_env('R2_ACCESS_KEY_ID')}/{scope}, SignedHeaders={signed}, Signature={signature}"
    send_headers = {name: value for name, value in headers.items() if name != "host"}  # httpx sets Host itself
    try:
        response = httpx.put(endpoint + quote(path, safe="/-_.~"), content=data, headers=send_headers, timeout=30)
    except httpx.HTTPError as exc:
        raise StorageError(f"storage upload failed: {type(exc).__name__}") from exc
    if response.status_code >= 300:
        raise StorageError(f"storage upload failed (HTTP {response.status_code})")


def presign_get(key: str, expires: int = 600, *, now: datetime | None = None) -> str:
    """A time-limited GET URL for `key` (query-string SigV4). Raises StorageError when storage is not configured."""
    if not r2_configured():
        raise StorageError("object storage is not configured")
    endpoint, host, path = _object_url_parts(key)
    amz_date = _stamp(now)
    scope = f"{amz_date[:8]}/{REGION}/{SERVICE}/aws4_request"
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{_env('R2_ACCESS_KEY_ID')}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(expires)),
        "X-Amz-SignedHeaders": "host",
    }
    canonical, _ = canonical_request("GET", path, query, {"host": host}, UNSIGNED_PAYLOAD)
    signature = signature_for(canonical, secret=_env("R2_SECRET_ACCESS_KEY"), amz_date=amz_date, region=REGION, service=SERVICE)
    query_string = "&".join(f"{uri_encode(k)}={uri_encode(v)}" for k, v in sorted(query.items()))
    return f"{endpoint}{quote(path, safe='/-_.~')}?{query_string}&X-Amz-Signature={signature}"
