from __future__ import annotations

import os
import threading
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

FEATURES = {
    "inventory_list",
    "so_detail",
    "stock",
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
    "other",
}
ESSENTIAL_FEATURES = {"acknowledge", "lock_status", "so_detail"}
PHT = ZoneInfo("Asia/Manila")

_feature: ContextVar[str] = ContextVar("zoho_feature", default="other")
_lock = threading.Lock()
_day = ""
_total = 0
_by_feature: Counter[str] = Counter()
_by_hour: Counter[str] = Counter()
_warnings: set[str] = set()
_time_override: datetime | None = None


class ZohoBudgetGuard(Exception):
    pass


def _now() -> datetime:
    return _time_override or datetime.now(PHT)


def set_time_override(value: datetime | None) -> None:
    global _time_override
    _time_override = value


def budget() -> int:
    try:
        return max(1, int(os.environ.get("ZOHO_DAILY_BUDGET", "4000")))
    except ValueError:
        return 4000


def _rollover(now: datetime) -> None:
    global _day, _total, _by_feature, _by_hour, _warnings
    today = now.date().isoformat()
    if _day != today:
        _day = today
        _total = 0
        _by_feature = Counter()
        _by_hour = Counter()
        _warnings = set()


@contextmanager
def feature(name: str):
    token = _feature.set(name if name in FEATURES else "other")
    try:
        yield
    finally:
        _feature.reset(token)


def current_feature() -> str:
    return _feature.get() if _feature.get() in FEATURES else "other"


def is_working_time(now: datetime | None = None) -> bool:
    now = now or _now()
    return dtime(6, 0) <= now.timetz().replace(tzinfo=None) < dtime(20, 0)


def should_skip_background(feature_name: str | None = None, *, background: bool = False) -> bool:
    name = feature_name or current_feature()
    return background and name not in ESSENTIAL_FEATURES and not is_working_time()


def enforce_budget(feature_name: str | None = None) -> None:
    name = feature_name or current_feature()
    if name in ESSENTIAL_FEATURES:
        return
    snap = snapshot()
    used = snap["zoho_calls_today"]
    limit = snap["budget"]
    if used >= limit:
        raise ZohoBudgetGuard("Zoho daily budget exhausted; serving cache only.")
    if used >= int(limit * 0.8):
        raise ZohoBudgetGuard("Zoho daily budget guard active; non-essential Zoho calls are paused.")


def record_call(feature_name: str | None = None) -> dict:
    global _total
    now = _now()
    name = feature_name or current_feature()
    if name not in FEATURES:
        name = "other"
    with _lock:
        _rollover(now)
        _total += 1
        _by_feature[name] += 1
        _by_hour[now.strftime("%H")] += 1
        return _snapshot_locked(now)


def _snapshot_locked(now: datetime) -> dict:
    limit = budget()
    percent = (_total / limit * 100) if limit else 0
    return {
        "date": now.date().isoformat(),
        "budget": limit,
        "zoho_calls_today": _total,
        "by_feature": dict(_by_feature),
        "by_hour": dict(_by_hour),
        "percent_used": round(percent, 1),
        "guard": "exhausted" if percent >= 100 else "cache_only" if percent >= 80 else "warn" if percent >= 50 else "ok",
    }


def snapshot() -> dict:
    now = _now()
    with _lock:
        _rollover(now)
        return _snapshot_locked(now)


def reset() -> None:
    global _day, _total, _by_feature, _by_hour, _warnings
    with _lock:
        _day = ""
        _total = 0
        _by_feature = Counter()
        _by_hour = Counter()
        _warnings = set()
        _rollover(_now())
