"""Delivery write-back to Zoho Inventory after a crew action has COMMITTED (never inside the crew transaction, never blocking it).

  * A1 "Closed": a SALES ORDER stop (SO only; never WM / TO / PO) checked out as Delivered or Partially delivered gets the custom
    sub-status whose code is in env ZOHO_SO_CLOSED_SUBSTATUS (POST salesorders/{id}/substatus/{code}). Zoho Inventory has no API to
    set the system "Closed" status; without the env var the job is recorded as "skipped: not configured" and nothing is sent.
    "Cancelled by request" / Not delivered never close the SO.
  * A2 POD attachment: after a proof-of-delivery photo ("delivery_photo") of an SO stop is stored in Google Drive, the same compressed
    JPEG (same file name as on Drive) is attached to the SO (POST salesorders/{id}/attachment, multipart field "attachment").

Rules: every call goes through zoho_client (budget guard, limiter, usage counter, retries). Idempotent: a job's outcome is ONE
`trip_events` row (existing table) whose `idempotency_key` is `zoho-close:<so_id>` / `zoho-attach:<upload_key>`; the key is checked
before every send, so a replayed check-out / upload or a retried job never sends twice. A transient failure is retried in the
background (RETRY_DELAYS) and one "retrying" row is written so Driver Overview shows it; a Zoho 4xx (e.g. attachment size / count
limit) is final: logged + a "failed" row, the Drive copy stays. Pending retries are process memory (lost on restart).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

CLOSE_EVENT = "zoho_so_closed"
ATTACH_EVENT = "zoho_so_attachment"
SYSTEM_USER_ID = 0  # trip_events.user_id is NOT NULL; 0 = the system (no login has id 0)
SYSTEM_ROLE = "system"
RETRY_DELAYS = (60, 300, 900, 3600, 3 * 3600, 6 * 3600)  # seconds before attempt 2, 3, ...; then the job is given up
MAX_PENDING = 500
POD_FIELD_KEYS = frozenset({"delivery_photo"})
CLOSE_OUTCOMES = frozenset({"delivered", "partial"})


def closed_substatus() -> str:
    return os.environ.get("ZOHO_SO_CLOSED_SUBSTATUS", "").strip()


def attachments_enabled() -> bool:
    return os.environ.get("ZOHO_SO_ATTACH_POD", "1").strip().lower() not in ("0", "false", "no", "off")


def max_attachment_bytes() -> int:
    try:
        return int(os.environ.get("ZOHO_ATTACHMENT_MAX_BYTES", str(10 * 1024 * 1024)))
    except ValueError:
        return 10 * 1024 * 1024


def max_attachments_per_so() -> int:
    try:
        return int(os.environ.get("ZOHO_SO_MAX_ATTACHMENTS", "5"))
    except ValueError:
        return 5


@dataclass
class Job:
    kind: str  # close | attach
    key: str
    so_id: str
    batch_id: str
    so_number: str | None = None
    trip_id: int | None = None
    attempts: int = 0
    next_at: float = 0.0
    extra: dict = field(default_factory=dict)  # attach: photo_id, file_id, file_name, upload_key, data (first attempt only)


_jobs: dict[str, Job] = {}
_lock = threading.Lock()
_wake = threading.Event()
_worker_running = False


def _spawn(fn) -> None:
    """Run off the request (tests replace this)."""
    threading.Thread(target=fn, daemon=True, name="zoho-writeback").start()


def _open_session():
    from database import SessionLocal

    return SessionLocal()


def _now() -> float:
    return time.monotonic()


def pending() -> list[Job]:
    with _lock:
        return list(_jobs.values())


def reset() -> None:
    global _worker_running
    with _lock:
        _jobs.clear()
        _worker_running = False


def _enqueue(job: Job) -> None:
    global _worker_running
    with _lock:
        if job.key in _jobs:
            return
        if len(_jobs) >= MAX_PENDING:
            logger.warning("[ZOHO_WRITEBACK] queue full (%d); %s dropped", MAX_PENDING, job.key)
            return
        _jobs[job.key] = job
        start = not _worker_running
        _worker_running = True
    _wake.set()
    if start:
        _spawn(_worker)


def _worker() -> None:
    global _worker_running
    try:
        while True:
            wait = run_pending()
            with _lock:
                if not _jobs:
                    _worker_running = False
                    return
            _wake.clear()
            _wake.wait(timeout=max(1.0, wait if wait is not None else 60.0))
    except Exception:  # never let the thread die silently with the flag set
        logger.exception("[ZOHO_WRITEBACK] worker stopped")
        with _lock:
            _worker_running = False


def run_pending(*, force: bool = False) -> float | None:
    """Run every due job once (force: every job). Returns seconds until the next due job (None: queue empty)."""
    now = _now()
    with _lock:
        due = [j for j in _jobs.values() if force or j.next_at <= now]
    for job in due:
        _run(job)
    with _lock:
        if not _jobs:
            return None
        return max(0.0, min(j.next_at for j in _jobs.values()) - _now())


def _drop(job: Job) -> None:
    with _lock:
        _jobs.pop(job.key, None)


# ---- triggers (called after the crew / upload transaction committed; never raise) -----------------------------------------


def after_checkout(db, batch_id: str, so_id: str) -> None:
    """A1: queue the Zoho "Closed" sub-status for a sales order checked out Delivered / Partially delivered."""
    try:
        from models.sales_order_history import SalesOrderHistory
        from models.trip import Trip, TripStop
        from services import media_storage, order_types

        stop = db.execute(select(TripStop).join(Trip, Trip.id == TripStop.trip_id).where(Trip.batch_id == batch_id, TripStop.so_id == so_id)).scalars().first()
        if stop is None or stop.status != "checked_out" or media_storage.outcome_kind(stop.outcome) not in CLOSE_OUTCOMES:
            return
        row = db.get(SalesOrderHistory, so_id)
        if row is None or order_types.order_type_of(row) != "SO":
            return  # SO only: WM / TO / PO never touch Zoho here
        _enqueue(Job(kind="close", key=f"zoho-close:{so_id}", so_id=so_id, batch_id=batch_id, so_number=row.salesorder_number or stop.so_number, trip_id=stop.trip_id,
                     extra={"outcome": stop.outcome}))
    except Exception as exc:  # best effort: the check-out is committed
        logger.warning("[ZOHO_WRITEBACK] after_checkout skipped: %s", type(exc).__name__)


def after_upload(db, photo, *, file_name: str | None, data: bytes | None) -> None:
    """A2: queue the Zoho SO attachment of a stored proof-of-delivery photo (Drive only, SO only, once per upload_key)."""
    try:
        from models.sales_order_history import SalesOrderHistory
        from models.trip import Trip
        from services import media_storage, order_types

        if not attachments_enabled() or photo is None or not photo.so_id or not photo.upload_key or photo.field_key not in POD_FIELD_KEYS:
            return
        if not media_storage.is_drive_key(photo.storage_key):
            return
        row = db.get(SalesOrderHistory, photo.so_id)
        if row is None or order_types.order_type_of(row) != "SO":
            return
        trip_id = db.execute(select(Trip.id).where(Trip.batch_id == photo.batch_id)).scalar_one_or_none()
        _enqueue(Job(kind="attach", key=f"zoho-attach:{photo.upload_key}", so_id=photo.so_id, batch_id=photo.batch_id, so_number=row.salesorder_number, trip_id=trip_id,
                     extra={"photo_id": photo.id, "file_id": media_storage.drive_file_id(photo.storage_key), "file_name": file_name or f"{row.salesorder_number or photo.so_id}.jpg",
                            "upload_key": photo.upload_key, "size_bytes": photo.size_bytes, "data": data}))
    except Exception as exc:
        logger.warning("[ZOHO_WRITEBACK] after_upload skipped: %s", type(exc).__name__)


# ---- running one job ---------------------------------------------------------------------------------------------------------


class _Skip(Exception):
    pass


def _is_permanent(exc: Exception) -> bool:
    """A Zoho 4xx (validation, size / count limit, unknown sub-status) will fail the same way again."""
    text = str(exc)
    return "HTTP 4" in text and "HTTP 429" not in text


def _perform(job: Job, db) -> dict:
    from services import zoho_client

    if job.kind == "close":
        code = closed_substatus()
        if not code:
            raise _Skip("not configured (ZOHO_SO_CLOSED_SUBSTATUS is empty)")
        zoho_client.set_sales_order_substatus(job.so_id, code)
        return {"substatus": code}
    from models.trip import TripEvent
    from services import media_storage

    done = db.execute(select(func.count()).select_from(TripEvent).where(TripEvent.event_type == ATTACH_EVENT, TripEvent.so_id == job.so_id,
                                                                         TripEvent.idempotency_key.is_not(None))).scalar() or 0
    limit = max_attachments_per_so()
    if limit and done >= limit:
        raise _Skip(f"attachment limit reached ({limit} per sales order); the photo stays in Google Drive")
    data = job.extra.get("data")
    if data is None:
        data, _etag = media_storage.read_drive_photo(job.extra["file_id"], False)
    if len(data) > max_attachment_bytes():
        raise _Skip(f"photo is larger than the Zoho attachment limit ({len(data)} bytes); it stays in Google Drive")
    zoho_client.attach_to_sales_order(job.so_id, job.extra["file_name"], data)
    return {"file_name": job.extra["file_name"], "size_bytes": len(data)}


def _message(job: Job, status: str, detail: str | None) -> str:
    number = job.so_number or job.so_id
    if job.kind == "close":
        text = {"ok": f"Zoho: {number} set to Closed", "skipped": f"Zoho: {number} not closed ({detail})",
                "retrying": f"Zoho: closing {number} failed, retrying ({detail})", "failed": f"Zoho: closing {number} failed ({detail})"}
    else:
        text = {"ok": f"Zoho: proof photo attached to {number}", "skipped": f"Zoho: proof photo not attached to {number} ({detail})",
                "retrying": f"Zoho: attaching the proof photo to {number} failed, retrying ({detail})", "failed": f"Zoho: attaching the proof photo to {number} failed ({detail})"}
    return text[status]


def _record(db, job: Job, status: str, *, detail: str | None = None, extra: dict | None = None, keyed: bool) -> None:
    from models.trip import TripEvent
    from services import trip_events

    now = trip_events.now_utc()
    payload = {"so_number": job.so_number, "kind": job.kind, **{k: v for k, v in job.extra.items() if k != "data"}}
    result = {"status": status, "detail": detail, "attempts": job.attempts, "message": _message(job, status, detail), **(extra or {})}
    db.add(TripEvent(trip_id=job.trip_id, batch_id=job.batch_id, event_type=CLOSE_EVENT if job.kind == "close" else ATTACH_EVENT, so_id=job.so_id,
                     user_id=SYSTEM_USER_ID, role=SYSTEM_ROLE, staff_id=None, client_ts=now, received_at=now, gps=None,
                     idempotency_key=job.key if keyed else None, payload=payload, result=result))
    try:
        db.commit()
    except IntegrityError:  # another worker recorded this key first
        db.rollback()
        return
    trip_events.bump()


def _already_done(db, key: str) -> bool:
    from services import trip_events

    return trip_events.find_by_key(db, key) is not None


def _run(job: Job) -> None:
    try:
        with _open_session() as db:
            if _already_done(db, job.key):
                _drop(job)
                return
            job.attempts += 1
            try:
                result = _perform(job, db)
            except _Skip as skip:
                logger.info("[ZOHO_WRITEBACK] %s skipped: %s", job.key, skip)
                _record(db, job, "skipped", detail=str(skip), keyed=True)
                _drop(job)
                return
            except Exception as exc:  # noqa: BLE001 - Zoho / Drive failure: never surfaces to the crew
                detail = str(exc)[:300] or type(exc).__name__
                job.extra.pop("data", None)  # retries re-read the photo from Drive instead of holding bytes in memory
                if _is_permanent(exc) or job.attempts > len(RETRY_DELAYS):
                    logger.warning("[ZOHO_WRITEBACK] %s failed for good after %d attempt(s): %s", job.key, job.attempts, detail)
                    _record(db, job, "failed", detail=detail, keyed=False)
                    _drop(job)
                    return
                logger.warning("[ZOHO_WRITEBACK] %s attempt %d failed, retrying: %s", job.key, job.attempts, detail)
                if job.attempts == 1:  # one visible "retrying" row, not one per attempt
                    _record(db, job, "retrying", detail=detail, keyed=False)
                job.next_at = _now() + RETRY_DELAYS[job.attempts - 1]
                return
            _record(db, job, "ok", extra=result, keyed=True)
            logger.info("[ZOHO_WRITEBACK] %s done", job.key)
            _drop(job)
    except Exception:  # database unavailable: keep the job and try later
        logger.exception("[ZOHO_WRITEBACK] %s could not run", job.key)
        job.next_at = _now() + RETRY_DELAYS[min(max(job.attempts - 1, 0), len(RETRY_DELAYS) - 1)]


def timeline_label(event) -> str | None:
    """Text for the Driver Overview timeline of a write-back row."""
    return (event.result or {}).get("message")
