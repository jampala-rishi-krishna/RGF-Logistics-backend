"""Photo / signature upload (object storage only) and signed-URL redirect. Contract: docs/TRIP_WORKFLOW.md sections 4 and 9.

Bytes go to R2; Neon gets one `trip_photos` metadata row per upload. No Zoho, no local disk.
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, ROLE_ALIASES, require_role
from database import get_db
from models.trip import TripPhoto
from services import media_storage
from services.trip_events import fail
from services.trip_timeline import batch_crew, batch_rows, is_crew

router = APIRouter(tags=["media"])

MAX_BYTES = 5 * 1024 * 1024
ISSUE_BATCH = "issue"  # trip_photos.batch_id of a photo attached to a reported issue that is not tied to a trip
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


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


@router.post("/api/driver/uploads")
async def upload_photo(
    file: UploadFile = File(...),
    batch_id: str = Form(...),
    field_key: str = Form(...),
    upload_key: str = Form(...),
    so_id: str | None = Form(None),
    user: CurrentUser = Depends(require_role("driver", "helper")),
    db: Session = Depends(get_db),
):
    if not media_storage.is_enabled():
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

    storage_key = f"trips/{_SAFE.sub('_', batch_id)}/{uuid.uuid4().hex}.{EXTENSIONS[content_type]}"
    try:
        media_storage.put_object(storage_key, data, content_type)
    except media_storage.StorageError:
        fail(503, "The photo could not be stored. Try again.", "storage_disabled")
    photo = TripPhoto(batch_id=batch_id, so_id=so_id, field_key=(field_key or "photo")[:100], storage_key=storage_key, content_type=content_type, size_bytes=len(data), uploaded_by_user_id=user.id, upload_key=upload_key)
    db.add(photo)
    try:
        db.commit()
    except IntegrityError:  # a concurrent retry of the same upload_key won: return its row
        db.rollback()
        winner = db.execute(select(TripPhoto).where(TripPhoto.upload_key == upload_key)).scalar_one_or_none()
        if winner is None:
            raise
        return _photo_response(winner, replayed=True)
    return _photo_response(photo)


@router.get("/api/media/{photo_id}")
def get_media(photo_id: int, user: CurrentUser = Depends(require_role("driver", "helper", "logistics_officer", "admin", "dispatcher")), db: Session = Depends(get_db)):
    photo = db.get(TripPhoto, photo_id)
    if photo is None:
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
    if not media_storage.is_enabled():
        fail(503, "Photo storage is not set up yet.", "storage_disabled")
    return RedirectResponse(media_storage.presign_get(photo.storage_key, 600), status_code=302, headers={"Cache-Control": "private, no-store"})
