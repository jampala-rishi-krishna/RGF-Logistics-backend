"""Zoho API usage accounting.

Every Zoho HTTP attempt calls record_call() once. Counts live in memory as an unflushed delta
and are written to Neon (table zoho_api_usage) every FLUSH_SECONDS, only when the delta is
non-zero, with an additive UPSERT (count = count + EXCLUDED.count) - an absolute value is never
written. At startup start() loads today's rows back into memory before the first Zoho call is
allowed, so the counter and the budget guard survive restarts. If that load fails the guard
fails SAFE: only essential calls are allowed until the load succeeds (retried every 15s).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone, time as dtime
from zoneinfo import ZoneInfo

import sqlalchemy as sa

logger = logging.getLogger("zoho_usage")

FEATURES = {
    "inventory_list",
    "so_detail",
    "items_batch",
    "item_detail",
    "custom_view",
    "packages",
    "shipments",
    "lock_status",
    "acknowledge",
    "reports",
    "dashboard",
    "agent_tools",
    "fleet_sync",
    "prewarm",
    "token_refresh",
    "other",
}
# OAuth token refreshes hit accounts.zoho.com, not the API: tracked, but never part of the total or budget.
UNCOUNTED = {"token_refresh"}
ESSENTIAL_FEATURES = {"acknowledge", "lock_status", "so_detail"}
# While the startup restore has failed, only these pass freely; so_detail is hard-capped (see failsafe_so_detail_cap).
FAILSAFE_FREE_FEATURES = {"acknowledge", "lock_status"}
SOURCES = ("request", "background")
FLUSH_SECONDS = 60
RESTORE_RETRY_SECONDS = 15

_feature: ContextVar[str] = ContextVar("zoho_feature", default="other")
_label: ContextVar[str | None] = ContextVar("zoho_label", default=None)
_forced_background: ContextVar[bool] = ContextVar("zoho_forced_background", default=False)

_lock = threading.Lock()
_day = ""
_base: Counter = Counter()    # (category, source) -> count already in Neon for _day
_delta: Counter = Counter()   # (day, category, source) -> count not yet flushed
_enabled = False              # True once start() ran: the fail-safe guard only applies to the running service
_restored = False
_restored_total = 0
_restore_error: str | None = None
_time_override: datetime | None = None
_failsafe_so_detail = 0       # so_detail attempts made while the restore had failed
_last_flushed = 0
_last_readback = 0.0          # time.monotonic() of the last successful read of today's total from Neon
READBACK_STALE_SECONDS = 300
_flush_lock = threading.Lock()  # serialises flush(): the 60s loop and shutdown must never write the same delta twice
_store = None
_stop = threading.Event()
_thread: threading.Thread | None = None
PROCESS_STARTED_AT = datetime.now(timezone.utc)

_metadata = sa.MetaData()
usage_table = sa.Table(
    "zoho_api_usage", _metadata,
    sa.Column("usage_day", sa.Date, primary_key=True),
    sa.Column("category", sa.Text, primary_key=True),
    sa.Column("source", sa.Text, primary_key=True),
    sa.Column("count", sa.BigInteger, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


class ZohoBudgetGuard(Exception):
    pass


class DbStore:
    """Neon-backed storage. session_factory defaults to database.SessionLocal (imported lazily)."""

    def __init__(self, session_factory=None):
        self._factory = session_factory

    def _session(self):
        if self._factory is not None:
            return self._factory()
        import database
        return database.SessionLocal()

    def load(self, day: date) -> dict[tuple[str, str], int]:
        with self._session() as db:
            rows = db.execute(sa.select(usage_table.c.category, usage_table.c.source, usage_table.c.count)
                              .where(usage_table.c.usage_day == day)).all()
        return {(r[0], r[1]): int(r[2]) for r in rows}

    def write(self, rows: list[tuple[date, str, str, int]]) -> None:
        now = datetime.now(timezone.utc)
        with self._session() as db:
            if db.get_bind().dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            for day, category, source, count in rows:
                stmt = insert(usage_table).values(usage_day=day, category=category, source=source, count=count, updated_at=now)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["usage_day", "category", "source"],
                    set_={"count": usage_table.c.count + stmt.excluded.count, "updated_at": now},
                )
                db.execute(stmt)
            db.commit()


def set_store(store) -> None:
    global _store
    _store = store


def _get_store():
    global _store
    if _store is None:
        _store = DbStore()
    return _store


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.environ.get("ZOHO_USAGE_TZ", "").strip() or "Asia/Manila")
    except Exception:
        return ZoneInfo("Asia/Manila")


def timezone_name() -> str:
    return getattr(_tz(), "key", "Asia/Manila")


def _now() -> datetime:
    return _time_override or datetime.now(_tz())


def set_time_override(value: datetime | None) -> None:
    global _time_override
    _time_override = value


def budget() -> int:
    try:
        return max(1, int(os.environ.get("ZOHO_DAILY_BUDGET", "4000")))
    except ValueError:
        return 4000


def org_limit() -> int:
    try:
        return max(1, int(os.environ.get("ZOHO_ORG_DAILY_LIMIT", "10000")))
    except ValueError:
        return 10000


def failsafe_so_detail_cap() -> int:
    try:
        return max(0, int(os.environ.get("ZOHO_FAILSAFE_SO_DETAIL_CAP", "50")))
    except ValueError:
        return 50


def _today(now: datetime) -> str:
    return now.date().isoformat()


def _ensure_day_locked(now: datetime) -> None:
    global _day, _base
    today = _today(now)
    if _day != today:
        _day = today
        _base = Counter()  # yesterday's delta stays keyed by its own day and is still flushed


def _total_locked() -> int:
    total = sum(v for (cat, _), v in _base.items() if cat not in UNCOUNTED)
    total += sum(v for (day, cat, _), v in _delta.items() if day == _day and cat not in UNCOUNTED)
    return total


def _by_category_locked() -> tuple[dict, dict]:
    by_cat: Counter = Counter()
    by_source: Counter = Counter()
    for (cat, src), v in _base.items():
        by_cat[cat] += v
        if cat not in UNCOUNTED:
            by_source[src] += v
    for (day, cat, src), v in _delta.items():
        if day == _day:
            by_cat[cat] += v
            if cat not in UNCOUNTED:
                by_source[src] += v
    return by_cat, by_source


# --- feature labelling -------------------------------------------------------------------------

@contextmanager
def feature(name: str):
    token = _feature.set(name if name in FEATURES else "other")
    try:
        yield
    finally:
        _feature.reset(token)


@contextmanager
def labelled(name: str):
    """Count every Zoho call inside the block under `name` and as source=background
    (used by schedulers, e.g. fleet_sync). Budget-guard decisions still use the real feature."""
    label = _label.set(name if name in FEATURES else "other")
    forced = _forced_background.set(True)
    try:
        yield
    finally:
        _label.reset(label)
        _forced_background.reset(forced)


def current_feature() -> str:
    return _feature.get() if _feature.get() in FEATURES else "other"


def is_working_time(now: datetime | None = None) -> bool:
    now = now or _now()
    return dtime(6, 0) <= now.timetz().replace(tzinfo=None) < dtime(20, 0)


def should_skip_background(feature_name: str | None = None, *, background: bool = False) -> bool:
    name = feature_name or current_feature()
    return background and name not in ESSENTIAL_FEATURES and not is_working_time()


def guard_state_locked(total: int) -> str:
    limit = budget()
    if _enabled and not _restored:
        return "unknown_restoring"
    percent = total / limit * 100
    return "exhausted" if percent >= 100 else "cache_only" if percent >= 80 else "warn" if percent >= 50 else "ok"


def enforce_budget(feature_name: str | None = None) -> None:
    name = feature_name or current_feature()
    now = _now()
    with _lock:
        _ensure_day_locked(now)
        if _enabled and not _restored:
            if name in FAILSAFE_FREE_FEATURES:
                return
            if name == "so_detail" and _failsafe_so_detail < failsafe_so_detail_cap():
                return
            raise ZohoBudgetGuard("Zoho usage total not yet restored from the database; only acknowledge/lock calls "
                                  "(and a capped number of SO detail calls) are allowed.")
        if name in ESSENTIAL_FEATURES:
            return
        used = _total_locked()
    limit = budget()
    if used >= limit:
        raise ZohoBudgetGuard("Zoho daily budget exhausted; serving cache only.")
    if used >= int(limit * 0.8):
        raise ZohoBudgetGuard("Zoho daily budget guard active; non-essential Zoho calls are paused.")


def record_call(feature_name: str | None = None, *, background: bool = False) -> None:
    """Count ONE outgoing attempt (retries included). In-memory only; flushed in batches."""
    global _failsafe_so_detail
    now = _now()
    real = feature_name or current_feature()
    name = _label.get() or real
    if name not in FEATURES:
        name = "other"
    source = "background" if (background or _forced_background.get()) else "request"
    with _lock:
        _ensure_day_locked(now)
        _delta[(_day, name, source)] += 1  # bucketed by the day the call was MADE, not the flush time
        if _enabled and not _restored and real == "so_detail":
            _failsafe_so_detail += 1


# --- persistence -------------------------------------------------------------------------------

def restore() -> bool:
    """Load today's totals from Neon into memory. Additive with anything counted meanwhile."""
    global _base, _restored, _restored_total, _restore_error, _last_readback
    now = _now()
    with _lock:
        _ensure_day_locked(now)
        day = _day
    try:
        rows = _get_store().load(date.fromisoformat(day))
    except Exception as exc:
        with _lock:
            _restore_error = f"{type(exc).__name__}: {exc}"[:300]
        logger.error("[ZOHO_USAGE] could not restore today's usage from Neon (%s). Guard FAILS SAFE: only essential Zoho "
                     "calls are allowed until the restore succeeds; retrying every %ss.", type(exc).__name__, RESTORE_RETRY_SECONDS)
        return False
    with _lock:
        if _day == day:
            _base = Counter(rows)
        _last_readback = time.monotonic()
        _restored = True
        _restore_error = None
        _restored_total = sum(v for (cat, _), v in rows.items() if cat not in UNCOUNTED)
    return True


def flush() -> bool:
    """Write the unflushed delta additively (each row under the day its calls were made). On failure the
    delta stays in memory for the next cycle. After a successful write, today's total is re-read from Neon
    and replaces the in-memory baseline (this process's unflushed delta stays on top), so another instance's
    writes during a deploy overlap are picked up."""
    global _last_flushed
    with _flush_lock:
        with _lock:
            pending = {k: v for k, v in _delta.items() if v > 0}
        if not pending:
            return True
        rows = [(date.fromisoformat(day), cat, src, v) for (day, cat, src), v in sorted(pending.items())]
        try:
            _get_store().write(rows)
        except Exception as exc:
            logger.error("[ZOHO_USAGE] flush failed (%s); keeping %s unflushed calls for the next cycle.",
                         type(exc).__name__, sum(pending.values()))
            return False
        with _lock:
            _last_flushed = sum(pending.values())
            for key, v in pending.items():
                _delta[key] -= v
                if _delta[key] <= 0:
                    del _delta[key]
                day, cat, src = key
                if day == _day:
                    _base[(cat, src)] += v  # provisional, in case the read-back below fails
            day = _day
        _readback(day)
        return True


def _readback(day: str) -> None:
    """Replace the in-memory baseline with Neon's total for `day`; the unflushed delta stays on top."""
    global _base, _last_readback
    try:
        fresh = _get_store().load(date.fromisoformat(day))
    except Exception as exc:
        logger.warning("[ZOHO_USAGE] read-back failed (%s); keeping the local baseline.", type(exc).__name__)
        return
    with _lock:
        _last_readback = time.monotonic()
        if _day == day:
            _base = Counter(fresh)


def refresh_if_stale() -> None:
    """Called from /health: one read-back at most every READBACK_STALE_SECONDS, so an idle instance isn't stale."""
    global _last_readback
    with _lock:
        if not _restored or time.monotonic() - _last_readback < READBACK_STALE_SECONDS:
            return
        _last_readback = time.monotonic()  # claim the slot first: concurrent /health calls never double-read
        day = _day
    if day:
        _readback(day)


def _loop() -> None:
    while not _stop.wait(FLUSH_SECONDS if _restored else RESTORE_RETRY_SECONDS):
        try:
            if not _restored:
                restore()
            flush()
        except Exception:
            logger.exception("[ZOHO_USAGE] flush loop error")


def start(instance_id: str = "") -> dict:
    """Restore today's totals BEFORE any Zoho call, then start the 60s flusher."""
    global _enabled, _thread, _restored
    _enabled = True
    _restored = False  # nothing is trusted until today's totals are loaded
    restore()
    snap = snapshot()
    logger.info("[ZOHO_USAGE] startup instance_id=%s restored=%s restored_total=%s budget=%s budget_remaining=%s tz=%s",
                instance_id, snap["usage_restored_from_db"]["restored"], snap["usage_restored_from_db"]["total"],
                snap["budget"], max(0, snap["budget"] - snap["zoho_calls_today"]), snap["timezone"])
    _stop.clear()
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, name="zoho-usage-flusher", daemon=True)
        _thread.start()
    return snap


def stop() -> None:
    """Lifespan shutdown (runs on uvicorn's graceful shutdown after SIGTERM): stop the flusher, write the rest."""
    global _last_flushed
    _stop.set()
    if _thread is not None and _thread.is_alive() and _thread is not threading.current_thread():
        _thread.join(timeout=2)
    _last_flushed = 0
    ok = flush()
    if ok:
        logger.info("zoho_usage shutdown flush: %s calls persisted", _last_flushed)
    else:
        with _lock:
            left = sum(_delta.values())
        logger.error("zoho_usage shutdown flush FAILED: %s calls not persisted", left)


# --- reporting ---------------------------------------------------------------------------------

def snapshot() -> dict:
    now = _now()
    with _lock:
        _ensure_day_locked(now)
        total = _total_locked()
        by_cat, by_source = _by_category_locked()
        limit = budget()
        counted = {k: v for k, v in by_cat.items() if k not in UNCOUNTED}
        counted.setdefault("other", 0)
        counted.setdefault("lock_status", 0)
        return {
            "date": _day,
            "timezone": timezone_name(),
            "budget": limit,
            "org_limit": org_limit(),
            "zoho_calls_today": total,
            "by_feature": counted,
            "by_source": {"request": by_source.get("request", 0), "background": by_source.get("background", 0)},
            "token_refresh": by_cat.get("token_refresh", 0),
            "unflushed": sum(v for v in _delta.values()),
            "percent_used": round(total / limit * 100, 1),
            "guard": guard_state_locked(total),
            "scope": "neon_additive",
            "process_started_at": PROCESS_STARTED_AT.isoformat(),
            "usage_restored_from_db": {"restored": _restored, "total": _restored_total, "error": _restore_error},
        }


def reset(*, restored: bool = True) -> None:
    """Test helper: clear all memory state."""
    global _day, _base, _delta, _restored, _restored_total, _restore_error, _enabled, _failsafe_so_detail
    with _lock:
        _day = ""
        _base = Counter()
        _delta = Counter()
        _restored = restored
        _restored_total = 0
        _restore_error = None
        _enabled = False
        _failsafe_so_detail = 0
