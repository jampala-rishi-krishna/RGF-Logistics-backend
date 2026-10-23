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
import re
import threading
import time
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone, time as dtime, timedelta
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
    "delivery_writeback",  # services/zoho_writeback.py: SO Closed sub-status + POD attachment after a check-out
    "reports",
    "reconcile",
    "po_to",
    "dashboard",
    "agent_tools",
    "fleet_sync",
    "prewarm",
    "token_refresh",
    "rate_limited",
    "lock_webhook",
    "other",
}
# Tracked, but never part of the total or budget: OAuth token refreshes hit accounts.zoho.com, a 429 was
# rejected before Zoho processed it, the lock webhook POST is not an Inventory API call, and lock_status
# calls run under the separate lock credential (a different Zoho user), so they are not this app user's
# calls. They still count against the org-wide Zoho limit, which is why they stay tracked.
UNCOUNTED = {"token_refresh", "rate_limited", "lock_webhook", "lock_status"}
ESSENTIAL_FEATURES = {"acknowledge", "lock_status", "so_detail", "delivery_writeback"}
CAP_SETTINGS = {
    "items": ("ZOHO_CAP_ITEMS", {"items_batch", "item_detail"}),
    "reports": ("ZOHO_CAP_REPORTS", {"reports"}),
    "so_detail_reconcile": ("ZOHO_CAP_SO_DETAIL_RECONCILE", {"so_detail", "reconcile"}),
    "po_to": ("ZOHO_CAP_PO_TO", {"po_to"}),
}
# At 100% of the budget everything stops except these two (so_detail is essential only below 100%).
HARD_STOP_EXEMPT = {"acknowledge", "lock_status"}
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
_webhook_in: Counter = Counter()  # inbound webhook events; deliberately outside the Zoho API budget
_webhook_outcomes: Counter = Counter()
_shadow_counts: Counter = Counter()
_webhook_mapping_fixes = 0
_webhook_last_received: dict[str, str] = {}
_webhook_warned = False
_webhook_alert_sent = False
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


def _event_category(kind: str, value: str) -> str:
    return f"webhook_{kind}:{value}"


def _is_uncounted_category(category: str) -> bool:
    return category in UNCOUNTED or category.startswith("webhook_")


def _event_count_locked(kind: str, value: str) -> int:
    category = _event_category(kind, value)
    return sum(v for (cat, _), v in _base.items() if cat == category) + sum(
        v for (day, cat, _), v in _delta.items() if day == _day and cat == category
    )


def _rebuild_webhook_counters_locked() -> None:
    global _webhook_in, _webhook_outcomes, _shadow_counts, _webhook_mapping_fixes
    _webhook_in = Counter()
    _webhook_outcomes = Counter()
    _shadow_counts = Counter()
    _webhook_mapping_fixes = 0
    all_rows = list(_base.items()) + [((cat, src), value) for (day, cat, src), value in _delta.items() if day == _day]
    for (category, _source), value in all_rows:
        if category.startswith("webhook_in:"):
            _webhook_in[category.split(":", 1)[1]] += value
        elif category.startswith("webhook_outcome:"):
            _webhook_outcomes[category.split(":", 1)[1]] += value
        elif category.startswith("webhook_shadow:"):
            _shadow_counts[category.split(":", 1)[1]] += value
        elif category == "webhook_mapping:fix":
            _webhook_mapping_fixes += value


def _send_webhook_threshold_alert() -> None:
    """Best-effort alert using the existing logistics/procurement recipients."""
    try:
        from services import app_settings, gmail_sender
        raw = ",".join((app_settings.get("PICKUP_LOGISTICS_TO") or "", app_settings.get("PICKUP_PROCUREMENT_TO") or ""))
        recipients = [x.strip() for x in raw.split(",") if x.strip()]
        if not recipients:
            logger.warning("[ZOHO_WEBHOOK] 700/day threshold reached but no admin alert recipient is configured")
            return
        gmail_sender.send_email(
            to=recipients,
            subject="IntelliFleet Zoho webhook volume warning",
            text="IntelliFleet received 700 Zoho webhooks today (Asia/Manila). The 1,000/day org limit is shared with other integrations.",
            html="<p>IntelliFleet received 700 Zoho webhooks today (Asia/Manila).</p><p>The 1,000/day org limit is shared with other integrations.</p>",
            purpose="zoho-webhook-threshold",
            dedupe_key=f"zoho-webhook-threshold|{_day}",
            label_logistics=False,
        )
    except Exception:
        logger.exception("[ZOHO_WEBHOOK] threshold alert email failed")


def record_webhook(module: str, outcome: str | None = None, received_at: datetime | None = None) -> None:
    """Count an inbound webhook and persist the event in the existing usage table."""
    global _webhook_warned, _webhook_alert_sent
    now = received_at or _now()
    with _lock:
        _ensure_day_locked(now)
        module = str(module or "other")
        _delta[(_day, _event_category("in", module), "webhook")] += 1
        _webhook_in[str(module or "other")] += 1
        if outcome:
            _delta[(_day, _event_category("outcome", str(outcome)), "webhook")] += 1
            _webhook_outcomes[str(outcome)] += 1
        _webhook_last_received[module] = now.isoformat()
        total = sum(_webhook_in.values())
        if total >= 700 and not _webhook_warned:
            _webhook_warned = True
            logger.warning("[ZOHO_WEBHOOK] inbound webhook count reached %d", total)
        if total >= 700 and not _webhook_alert_sent and _event_count_locked("alert", "700") == 0:
            _webhook_alert_sent = True
            _delta[(_day, _event_category("alert", "700"), "webhook")] += 1
            threading.Thread(target=_send_webhook_threshold_alert, name="zoho-webhook-alert", daemon=True).start()


def record_shadow(kind: str) -> None:
    """Record a webhook shadow comparison result in memory only."""
    if kind not in {"shadow_ok", "shadow_mismatch", "shadow_missing"}:
        return
    with _lock:
        _ensure_day_locked(_now())
        _shadow_counts[kind] += 1
        _delta[(_day, _event_category("shadow", kind), "webhook")] += 1


def record_mapping_fix() -> None:
    global _webhook_mapping_fixes
    with _lock:
        _ensure_day_locked(_now())
        _webhook_mapping_fixes += 1
        _delta[(_day, _event_category("mapping", "fix"), "webhook")] += 1

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

    def history(self, days: int = 7, end: date | None = None) -> list[tuple[date, str, int]]:
        end = end or date.today()
        start = end - timedelta(days=max(1, days) - 1)
        with self._session() as db:
            rows = db.execute(
                sa.select(usage_table.c.usage_day, usage_table.c.category, sa.func.sum(usage_table.c.count))
                .where(usage_table.c.usage_day >= start, usage_table.c.usage_day <= end)
                .group_by(usage_table.c.usage_day, usage_table.c.category)
            ).all()
        return [(r[0], r[1], int(r[2])) for r in rows]

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
    # Admin override (Admin > Settings) > ZOHO_DAILY_BUDGET env > 4500. Memory-only lookup: safe on the per-call guard path.
    from services import app_settings

    return max(1, app_settings.get_int("ZOHO_DAILY_BUDGET"))


def org_limit() -> int:
    try:
        return max(1, int(re.sub(r"[,\s\u00a0\u202f]", "", os.environ.get("ZOHO_ORG_DAILY_LIMIT", "10000"))))
    except ValueError:
        return 10000


def failsafe_so_detail_cap() -> int:
    from services import app_settings

    return max(0, app_settings.get_int("ZOHO_FAILSAFE_SO_DETAIL_CAP"))


def _today(now: datetime) -> str:
    return now.date().isoformat()


def _ensure_day_locked(now: datetime) -> None:
    global _day, _base, _webhook_in, _webhook_outcomes, _shadow_counts, _webhook_mapping_fixes, _webhook_last_received, _webhook_warned, _webhook_alert_sent
    today = _today(now)
    if _day != today:
        _day = today
        _base = Counter()  # yesterday's delta stays keyed by its own day and is still flushed
        _rebuild_webhook_counters_locked()
        _webhook_last_received = {}
        _webhook_warned = False
        _webhook_alert_sent = False


def _total_locked() -> int:
    total = sum(v for (cat, _), v in _base.items() if not _is_uncounted_category(cat))
    total += sum(v for (day, cat, _), v in _delta.items() if day == _day and not _is_uncounted_category(cat))
    return total


def _by_category_locked() -> tuple[dict, dict]:
    by_cat: Counter = Counter()
    by_source: Counter = Counter()
    for (cat, src), v in _base.items():
        by_cat[cat] += v
        if not _is_uncounted_category(cat):
            by_source[src] += v
    for (day, cat, src), v in _delta.items():
        if day == _day:
            by_cat[cat] += v
            if not _is_uncounted_category(cat):
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


def cache_only_pct() -> float:
    """Percent of the budget at which non-essential calls pause (Admin > Settings ZOHO_GUARD_CACHE_ONLY_PCT, default 92)."""
    from services import app_settings

    return max(50, min(99, float(app_settings.get("ZOHO_GUARD_CACHE_ONLY_PCT"))))


def cache_only_limit() -> int:
    return round(budget() * cache_only_pct() / 100)


def _category_for_feature(name: str) -> str | None:
    return next((category for category, (_setting, features) in CAP_SETTINGS.items() if name in features), None)


def category_caps() -> dict[str, dict[str, int]]:
    from services import app_settings
    with _lock:
        return _category_caps_locked(app_settings)


def _category_caps_locked(app_settings) -> dict[str, dict[str, int]]:
    by_cat, _ = _by_category_locked()
    return {category: {"used": sum(by_cat.get(feature, 0) for feature in features), "cap": app_settings.get_int(setting)}
            for category, (setting, features) in CAP_SETTINGS.items()}


def guard_state_locked(total: int) -> str:
    limit = budget()
    if _enabled and not _restored:
        return "unknown_restoring"
    percent = total / limit * 100
    return "exhausted" if total >= limit else "cache_only" if total >= cache_only_limit() else "warn" if percent >= 50 else "ok"


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
        if name in HARD_STOP_EXEMPT:
            return
        used = _total_locked()
        category = _category_for_feature(name)
        if category:
            setting, features = CAP_SETTINGS[category]
            from services import app_settings
            category_used = sum(_by_category_locked()[0].get(feature, 0) for feature in features)
            if category_used >= app_settings.get_int(setting):
                raise ZohoBudgetGuard(f"Zoho {category} category cap reached; serving cache only.")
    limit = budget()
    if used >= limit:  # 100%: hard stop for everything except acknowledge / lock
        raise ZohoBudgetGuard("Zoho daily budget exhausted; serving cache only.")
    if name in ESSENTIAL_FEATURES:
        return
    if used >= cache_only_limit():
        raise ZohoBudgetGuard("Zoho daily budget guard active; non-essential Zoho calls are paused.")


def record_call(feature_name: str | None = None, *, background: bool = False) -> None:
    """Count ONE outgoing attempt (retries included). In-memory only; flushed in batches."""
    global _failsafe_so_detail
    now = _now()
    real = feature_name or current_feature()
    name = real if real in UNCOUNTED else (_label.get() or real)  # a scheduler label never turns a 429 into a counted call
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
    global _base, _webhook_in, _webhook_outcomes, _shadow_counts, _webhook_mapping_fixes, _webhook_alert_sent, _webhook_warned, _restored, _restored_total, _restore_error, _last_readback
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
        _rebuild_webhook_counters_locked()
        _last_readback = time.monotonic()
        _restored = True
        _restore_error = None
        _webhook_alert_sent = False
        for (category, _source), value in rows.items():
            if category == "webhook_alert:700" and value > 0:
                _webhook_alert_sent = True
        _webhook_warned = sum(_webhook_in.values()) >= 700
        _restored_total = sum(v for (cat, _), v in rows.items() if not _is_uncounted_category(cat))
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
        counted = {k: v for k, v in by_cat.items() if not _is_uncounted_category(k)}
        counted.setdefault("other", 0)
        return {
            "date": _day,
            "timezone": timezone_name(),
            "budget": limit,
            "cache_only_limit": cache_only_limit(),
            "org_limit": org_limit(),
            "zoho_calls_today": total,
            "by_feature": counted,
            "by_source": {"request": by_source.get("request", 0), "background": by_source.get("background", 0)},
            "token_refresh": by_cat.get("token_refresh", 0),
            "not_counted": {k: by_cat.get(k, 0) for k in ("rate_limited", "lock_webhook", "lock_status")},
            "webhook_in": dict(_webhook_in),
            "webhook_outcomes": dict(_webhook_outcomes),
            "shadow_ok": _shadow_counts.get("shadow_ok", 0),
            "shadow_mismatch": _shadow_counts.get("shadow_mismatch", 0),
            "shadow_missing": _shadow_counts.get("shadow_missing", 0),
            "unflushed": sum(v for v in _delta.values()),
            "percent_used": round(total / limit * 100, 1),
            "guard": guard_state_locked(total),
            "category_caps": _category_caps_locked(__import__("services.app_settings", fromlist=["get_int"])),
            "scope": "neon_additive",
            "process_started_at": PROCESS_STARTED_AT.isoformat(),
            "usage_restored_from_db": {"restored": _restored, "total": _restored_total, "error": _restore_error},
            "webhook_last_received": dict(_webhook_last_received),
            "webhook_mapping_fixes": _webhook_mapping_fixes,
        }


def webhook_history(days: int = 7) -> list[dict]:
    """Return persisted inbound webhook totals by Manila day and module."""
    try:
        rows = _get_store().history(days, end=_now().date())
    except (AttributeError, TypeError):
        return []
    modules = {"salesorder", "purchaseorder", "transferorder", "package", "lock-result"}
    grouped: dict[str, dict[str, int]] = {}
    for day, category, count in rows:
        if category.startswith("webhook_in:"):
            module = category.split(":", 1)[1]
            if module in modules:
                grouped.setdefault(day.isoformat(), {})[module] = count
    return [{"date": day, "total": sum(values.values()), "modules": values} for day, values in sorted(grouped.items(), reverse=True)]


def reset(*, restored: bool = True) -> None:
    """Test helper: clear all memory state."""
    global _day, _base, _delta, _webhook_in, _webhook_outcomes, _shadow_counts, _webhook_mapping_fixes, _webhook_last_received, _webhook_warned, _webhook_alert_sent, _restored, _restored_total, _restore_error, _enabled, _failsafe_so_detail
    with _lock:
        _day = ""
        _base = Counter()
        _delta = Counter()
        _webhook_in = Counter()
        _webhook_outcomes = Counter()
        _shadow_counts = Counter()
        _webhook_mapping_fixes = 0
        _webhook_last_received = {}
        _webhook_warned = False
        _webhook_alert_sent = False
        _restored = restored
        _restored_total = 0
        _restore_error = None
        _enabled = False
        _failsafe_so_detail = 0
