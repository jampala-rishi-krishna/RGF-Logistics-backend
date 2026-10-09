from __future__ import annotations

import os
import json
import tempfile
import threading
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, time as dtime
from pathlib import Path
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
_loaded = False


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


def _state_path() -> Path:
    configured = os.environ.get("ZOHO_USAGE_STATE_FILE", "").strip()
    if configured:
        return Path(configured)
    state_dir = os.environ.get("ZOHO_USAGE_STATE_DIR", "").strip()
    if not state_dir:
        # Render persistent disks are commonly mounted outside the app directory. If
        # one is configured, use it automatically; otherwise fall back to a local
        # runtime file so local restarts do not reset the admin widget.
        state_dir = os.environ.get("RENDER_DISK_MOUNT_PATH", "").strip()
    if not state_dir:
        state_dir = str(Path(__file__).resolve().parents[2] / ".runtime")
    return Path(state_dir) / "zoho_usage_state.json"


def _load_locked(now: datetime) -> None:
    global _loaded, _day, _total, _by_feature, _by_hour, _warnings
    if _loaded:
        return
    _loaded = True
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception:
        return
    if raw.get("date") != now.date().isoformat():
        return
    try:
        _day = raw["date"]
        _total = int(raw.get("zoho_calls_today") or 0)
        _by_feature = Counter({str(k): int(v) for k, v in (raw.get("by_feature") or {}).items()})
        _by_hour = Counter({str(k): int(v) for k, v in (raw.get("by_hour") or {}).items()})
        _warnings = set(raw.get("warnings") or [])
    except Exception:
        _day = ""
        _total = 0
        _by_feature = Counter()
        _by_hour = Counter()
        _warnings = set()


def _persist_locked(now: datetime) -> None:
    path = _state_path()
    payload = {
        "date": now.date().isoformat(),
        "zoho_calls_today": _total,
        "by_feature": dict(_by_feature),
        "by_hour": dict(_by_hour),
        "warnings": sorted(_warnings),
        "updated_at": now.isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as fh:
            json.dump(payload, fh, sort_keys=True)
            tmp = Path(fh.name)
        tmp.replace(path)
    except Exception:
        # Usage accounting must never break a Zoho request. The admin widget will
        # still show the in-memory count for this process if persistence fails.
        return


def _rollover(now: datetime) -> None:
    global _day, _total, _by_feature, _by_hour, _warnings
    _load_locked(now)
    today = now.date().isoformat()
    if _day != today:
        _day = today
        _total = 0
        _by_feature = Counter()
        _by_hour = Counter()
        _warnings = set()
        _persist_locked(now)


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
        _persist_locked(now)
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
        "scope": "persisted_local_day",
        "state_file": str(_state_path()),
    }


def snapshot() -> dict:
    now = _now()
    with _lock:
        _rollover(now)
        return _snapshot_locked(now)


def reset() -> None:
    global _day, _total, _by_feature, _by_hour, _warnings, _loaded
    with _lock:
        _loaded = True
        _day = ""
        _total = 0
        _by_feature = Counter()
        _by_hour = Counter()
        _warnings = set()
        _rollover(_now())
        _persist_locked(_now())
