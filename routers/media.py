"""Photo / signature upload and viewing. Contracts: docs/GDRIVE_MEDIA.md (Google Drive, the default) and docs/TRIP_WORKFLOW.md
sections 4 and 9 (request / response shape, R2).

Bytes go to the storage provider (services/media_storage.py: Drive or R2); Neon gets one `trip_photos` metadata row per upload.
No Zoho, no local disk. With Drive the server re-encodes the photo (JPEG, <= 1600px, no EXIF) and names / describes it from the
batch it already holds; viewing streams the bytes through this API (never a google.com redirect, never a public link).
"""
from __future__ import annotations

import logging
import re
import uuid

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, ROLE_ALIASES, require_role
from database import get_db
from models.trip import TripPhoto
from services import gdrive_storage, image_processing, media_storage, trip_events
from services.trip_events import fail
from services.trip_timeline import batch_crew, batch_rows, is_crew

logger = logging.getLogger(__name__)

router = APIRouter(tags=["media"])

MAX_BYTES = 5 * 1024 * 1024
ISSUE_BATCH = media_storage.ISSUE_BATCH  # trip_photos.batch_id of a photo attached to a reported issue that is not tied to a trip
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
VIEW_CACHE_CONTROL = "private, max-age=86400"


def sniff_image(data: bytes) -> str | None:
    """Content type from the file's magic bytes (the client-sent type / filename is never trusted)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _photo_response(photo: TripPhoto, replayed: bool = False) -> dict:
    return {"photo_id": photo.id, "content_type": photo.content_type, "size_bytes": photo.size_bytes, "replayed": replayed}


def _drive_store(meta: media_storage.PhotoMeta, upload_key: str, captured, jpeg: bytes) -> str:
    """storage_key of the photo in Drive (threadpool: Drive calls block). 503 keeps the photo queued on the phone."""
    return _drive_store_named(meta, upload_key, captured, jpeg)[0]


def _drive_store_named(meta: media_storage.PhotoMeta, upload_key: str, captured, jpeg: bytes) -> tuple[str, str | None]:
    """(storage_key, Drive file name) - the name is reused for the Zoho SO attachment (services/zoho_writeback.py)."""
    try:
        stored = media_storage.store_drive_photo(meta, upload_key, captured, jpeg)
    except gdrive_storage.DriveAuthError:
        fail(503, "Photo storage needs to be reconnected. The photo stays on the phone and will be sent later.", "storage_disabled")
    except gdrive_storage.DriveError:
        fail(503, "The photo could not be stored. Try again.", "storage_disabled")
    return f"{media_storage.DRIVE_PREFIX}{stored['id']}", stored.get("name")


@router.post("/api/driver/uploads")
async def upload_photo(
    file: UploadFile = File(...),
    batch_id: str = Form(...),
    field_key: str = Form(...),
    upload_key: str = Form(...),
    so_id: str | None = Form(None),
    captured_at: str | None = Form(None),  # phone capture time (ISO); validated like every client_ts, else the server receipt time
    user: CurrentUser = Depends(require_role("driver", "helper")),
    db: Session = Depends(get_db),
):
    provider = media_storage.provider()
    if provider == "none":
        fail(503, "Photo storage is not set up yet.", "storage_disabled")
    upload_key = (upload_key or "").strip()
    if not upload_key or len(upload_key) > 100:
        fail(422, "upload_key is required.", "validation", [{"key": "upload_key", "message": "A client-generated id is required."}])
    if field_key == "issue_photo" and batch_id in ("", ISSUE_BATCH, "profile"):
        batch_id = ISSUE_BATCH  # an issue photo from the profile: not tied to a trip, only its uploader and dispatch may open it
    else:
        crew = batch_crew(db, batch_id)
        if crew is None or not is_crew(crew, user.staff_id):
            fail(404, "Assignment not found.", "not_found")

    existing = db.execute(select(TripPhoto).where(TripPhoto.upload_key == upload_key)).scalar_one_or_none()
    if existing is not None:
        if existing.batch_id != batch_id or existing.uploaded_by_user_id != user.id:
            fail(409, "This upload key was already used.", "conflict")
        return _photo_response(existing, replayed=True)

    so_id = (so_id or "").strip() or None
    if so_id is not None:
        known = {row.id for row in batch_rows(db, batch_id)}
        if known and so_id not in known:
            fail(422, "That SO is not part of this batch.", "validation", [{"key": "so_id", "message": "Unknown SO."}])
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        fail(413, "The photo is larger than 5 MB.", "validation", [{"key": "file", "message": "Maximum size is 5 MB."}])
    content_type = sniff_image(data)
    if content_type is None:
        fail(422, "Only JPEG, PNG or WebP images are accepted.", "validation", [{"key": "file", "message": "Unsupported image type."}])
    field_key = (field_key or "photo")[:100]
    received = trip_events.now_utc()
    captured, _adjusted = trip_events.parse_client_ts((captured_at or "").strip()[:64] or None, received)

    if provider == "gdrive":
        try:
            jpeg, _width, _height = await run_in_threadpool(image_processing.reencode_jpeg, data)
        except image_processing.ImageError:
            fail(422, "The photo could not be read. Take it again.", "validation", [{"key": "file", "message": "Unreadable image."}])
        meta = media_storage.photo_meta(db, batch_id=batch_id, so_id=so_id, field_key=field_key, uploader_staff_id=user.staff_id)
        storage_key, drive_name = await run_in_threadpool(_drive_store_named, meta, upload_key, captured, jpeg)
        content_type, size = "image/jpeg", len(jpeg)
    else:
        drive_name = jpeg = None
        storage_key = f"trips/{_SAFE.sub('_', batch_id)}/{uuid.uuid4().hex}.{EXTENSIONS[content_type]}"
        try:
            media_storage.put_object(storage_key, data, content_type)
        except media_storage.StorageError:
            fail(503, "The photo could not be stored. Try again.", "storage_disabled")
        size = len(data)
    # Existing timestamp preserves camera time across offline sync; old rows keep upload time.
    photo = TripPhoto(batch_id=batch_id, so_id=so_id, field_key=field_key, storage_key=storage_key, content_type=content_type, size_bytes=size, uploaded_by_user_id=user.id, upload_key=upload_key, created_at=captured)
    db.add(photo)
    try:
        db.commit()
    except IntegrityError:  # a concurrent retry of the same upload_key won (it may have reused the same Drive file): return its row
        db.rollback()
        winner = db.execute(select(TripPhoto).where(TripPhoto.upload_key == upload_key)).scalar_one_or_none()
        if winner is None:
            raise
        return _photo_response(winner, replayed=True)
    from services import zoho_writeback
    zoho_writeback.after_upload(db, photo, file_name=drive_name, data=jpeg)  # committed: SO proof photo -> Zoho SO attachment, background (never raises)
    return _photo_response(photo)


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    bare = etag.strip('"')
    return any(part.strip().removeprefix("W/").strip('"') == bare for part in header.split(","))


def _stream_drive(file_id: str, thumb: bool, if_none_match: str | None) -> Response:
    headers = {"Cache-Control": VIEW_CACHE_CONTROL}
    try:
        if if_none_match:
            etag = media_storage.known_etag(file_id, thumb, ask_drive=True)  # a revalidation costs at most one metadata call, never the bytes
            if etag and _etag_matches(if_none_match, etag):
                return Response(status_code=304, headers={**headers, "ETag": etag})
        data, etag = media_storage.read_drive_photo(file_id, thumb)
    except gdrive_storage.DriveNotFound:
        fail(404, "Photo not found.", "not_found")
    except gdrive_storage.DriveError:
        fail(503, "Photo storage is not reachable right now.", "storage_disabled")
    except image_processing.ImageError:
        fail(404, "Photo not found.", "not_found")
    if _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers={**headers, "ETag": etag})
    return Response(content=data, media_type="image/jpeg", headers={**headers, "ETag": etag})


@router.get("/api/media/{photo_id}")
def get_media(photo_id: int, size: str = Query("full", pattern="^(full|thumb)$"), if_none_match: str | None = Header(None),
              user: CurrentUser = Depends(require_role("driver", "helper", "logistics_officer", "admin", "dispatcher", "sales", "wet_market")), db: Session = Depends(get_db), read_only: bool = Query(False)):
    photo = db.get(TripPhoto, photo_id)
    if photo is None:
        fail(404, "Photo not found.", "not_found")
    if user.role in {"sales", "wet_market"}:
        from services import order_tracker
        # No new DB lookup: use the photo already loaded by this existing path.
        row = live_sales_order_for_photo(photo)
        if row is None or not order_tracker.allowed(user, row):
            fail(404, "Photo not found.", "not_found")
        card = order_tracker.order_card(row)
        if photo.id not in {p["photo_id"] for p in card["sections"]["proof"]["value"]["photos"]}:
            fail(404, "Photo not found.", "not_found")
    if ROLE_ALIASES.get(user.role, user.role) in ("driver", "helper"):
        if photo.batch_id == ISSUE_BATCH:
            if photo.uploaded_by_user_id != user.id:
                fail(404, "Photo not found.", "not_found")
        else:
            crew = batch_crew(db, photo.batch_id)
            if crew is None or not is_crew(crew, user.staff_id):
                if photo.uploaded_by_user_id != user.id:
                    fail(404, "Photo not found.", "not_found")  # same answer as "does not exist"
    if media_storage.is_drive_key(photo.storage_key):
        if not gdrive_storage.configured():
            fail(503, "Photo storage is not set up yet.", "storage_disabled")
        file_id = media_storage.drive_file_id(photo.storage_key)
        tracker_read = read_only is True or user.role in {"sales", "wet_market"}
        if not tracker_read:
            media_storage.retry_pending(file_id)  # legacy crew/office reads may retry a failed name / description update
        response = _stream_drive(file_id, size == "thumb", if_none_match)
        if tracker_read:
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Vary"] = "Authorization"
        return response
    if not media_storage.r2_configured():  # an R2 photo (plain key or "r2:<key>") resolves through R2 whatever MEDIA_STORAGE says
        fail(503, "Photo storage is not set up yet.", "storage_disabled")
    return RedirectResponse(media_storage.presign_get(media_storage.r2_object_key(photo.storage_key), 600), status_code=302, headers={"Cache-Control": "private, no-store"})


def live_sales_order_for_photo(photo):
    from services import live_sales_order_cache, order_tracker
    row = live_sales_order_cache.find_cached(str(photo.so_id)) if photo.so_id else None
    return order_tracker.project(row) if row is not None else None
