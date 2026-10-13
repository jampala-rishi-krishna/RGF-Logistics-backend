"""Admin-editable, NON-SECRET settings (table `app_settings`, migration p6b7c8d9e0f1).

Resolution order for every key: admin override (this table) > environment variable > built-in default.
The table is read ONCE at startup into memory and written only when an admin saves a setting; every lookup is a
dict read, so it is safe on hot paths such as the Zoho budget guard. Only the fixed whitelist below can be stored:
secrets (R2, Zoho, Gmail, JWT, Cartrack, database) stay in environment variables and can never be put here.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("app_settings")


class SettingError(ValueError):
    """A submitted value is not acceptable for that key (shown to the admin as-is)."""


@dataclass(frozen=True)
class SettingDef:
    key: str  # also the environment variable name used as the fallback
    label: str
    description: str
    kind: str  # "int" | "phone"
    default: Any
    minimum: int | None = None
    maximum: int | None = None
    unit: str | None = None


DEFINITIONS: dict[str, SettingDef] = {d.key: d for d in (
    SettingDef("DISPATCH_PHONE", "Dispatch phone", "Number the driver app's 'Call dispatch' button dials. Leave empty to hide the button.", "phone", ""),
    SettingDef("DRIVER_STALE_MINUTES", "Driver stale threshold", "A driver on a trip with no action for this long is flagged stale in Driver Overview.", "int", 15, 1, 1440, "minutes"),
    SettingDef("ZOHO_DAILY_BUDGET", "Zoho daily budget", "Platform soft budget of Zoho API calls per day. Once usage reaches the cache-only percentage non-essential Zoho calls pause; at 100% everything stops except acknowledge and lock calls. Cannot exceed the organisation limit.", "int", 4500, 100, None, "calls/day"),
    SettingDef("ZOHO_GUARD_CACHE_ONLY_PCT", "Zoho guard: cache-only at", "Percentage of the daily budget at which non-essential Zoho calls (order lists, item details, ...) pause and the app serves cached data. Sales Order details, acknowledge and lock calls still run until 100%.", "int", 92, 50, 99, "% of budget"),
    SettingDef("ZOHO_FAILSAFE_SO_DETAIL_CAP", "Zoho fail-safe SO-detail cap", "While today's Zoho usage total could not be restored from the database, at most this many Sales Order detail calls are allowed.", "int", 50, 0, 1000, "calls"),
)}

_PHONE = re.compile(r"^[0-9+()\-\s]{0,32}$")
_NUMBER_NOISE = re.compile(r"[,\s\u00a0\u202f]")
_warned: set[str] = set()
_lock = threading.Lock()
_overrides: dict[str, str] = {}
_meta: dict[str, dict] = {}


def clean_number(raw: Any) -> str:
    """"4,600", " 4 600 " and "4\u00a0600" all mean 4600: thousands separators and spaces are stripped before parsing."""
    return _NUMBER_NOISE.sub("", "" if raw is None else str(raw))


def _warn_once(token: str, message: str, *args) -> None:
    """Log a configuration problem ONCE per process (i.e. once per startup), never on every read of the setting."""
    with _lock:
        if token in _warned:
            return
        _warned.add(token)
    logger.warning(message, *args)


def _org_limit() -> int:
    try:
        return max(1, int(clean_number(os.environ.get("ZOHO_ORG_DAILY_LIMIT", "10000"))))
    except ValueError:
        return 10000


def _bounds(definition: SettingDef) -> tuple[int | None, int | None]:
    if definition.key == "ZOHO_DAILY_BUDGET":
        return definition.minimum, _org_limit()
    return definition.minimum, definition.maximum


def parse(definition: SettingDef, raw: Any) -> int | str:
    """Validate and normalise a submitted value. Raises SettingError with a human message."""
    if definition.kind == "phone":
        text = "" if raw is None else str(raw).strip()
        if not _PHONE.match(text):
            raise SettingError(f"{definition.label} may contain only digits, +, -, spaces and brackets (max 32 characters).")
        return text
    if isinstance(raw, bool) or raw is None or not clean_number(raw):
        raise SettingError(f"{definition.label} must be a whole number.")
    try:
        number = int(clean_number(raw))
    except ValueError as exc:
        raise SettingError(f"{definition.label} must be a whole number.") from exc
    low, high = _bounds(definition)
    if low is not None and number < low:
        raise SettingError(f"{definition.label} must be at least {low}.")
    if high is not None and number > high:
        raise SettingError(f"{definition.label} must be at most {high}.")
    return number


def _env_or_default(definition: SettingDef) -> int | str:
    """Environment fallback. Deliberately as permissive as the old os.environ reads: an integer is taken as it is
    (callers apply their own floor); only the admin form enforces the tighter bounds."""
    raw = os.environ.get(definition.key)
    if raw is None:
        return definition.default
    if definition.kind == "phone":
        text = raw.strip()
        if _PHONE.match(text):
            return text
    elif clean_number(raw):
        try:
            return int(clean_number(raw))
        except ValueError:
            pass
    else:
        return definition.default
    _warn_once(f"env:{definition.key}", "[SETTINGS] environment value for %s is invalid; using the built-in default (this is logged once per startup)", definition.key)
    return definition.default


def get(key: str) -> int | str:
    """Effective value: admin override > environment variable > built-in default. Memory only, never touches the DB."""
    definition = DEFINITIONS[key]
    stored = _overrides.get(key)
    if stored is not None:
        try:
            return parse(definition, stored)
        except SettingError:
            _warn_once(f"stored:{key}", "[SETTINGS] stored value for %s is invalid; ignoring the override (this is logged once per startup)", key)
    return _env_or_default(definition)


def get_int(key: str) -> int:
    return int(get(key))


def get_str(key: str) -> str:
    return str(get(key))


def load(session_factory=None) -> bool:
    """Read every override from Neon into memory (the ONLY read; called once at startup). On any failure the
    environment values stay in force and the reason is logged."""
    try:
        from sqlalchemy import select

        from models.app_setting import AppSetting

        if session_factory is None:
            from database import SessionLocal as session_factory
        with session_factory() as db:
            rows = db.execute(select(AppSetting)).scalars().all()
    except Exception as exc:  # noqa: BLE001
        logger.error("[SETTINGS] could not load overrides from Neon (%s); environment values stay in force", type(exc).__name__)
        return False
    overrides: dict[str, str] = {}
    meta: dict[str, dict] = {}
    for row in rows:
        definition = DEFINITIONS.get(row.key)
        if definition is None:
            logger.warning("[SETTINGS] ignoring unknown stored setting %r", row.key)
            continue
        try:
            parse(definition, row.value)
        except SettingError:
            logger.warning("[SETTINGS] ignoring invalid stored value for %s", row.key)
            continue
        overrides[row.key] = row.value
        meta[row.key] = {"updated_by": row.updated_by, "updated_at": row.updated_at}
    with _lock:
        _overrides.clear()
        _overrides.update(overrides)
        _meta.clear()
        _meta.update(meta)
    logger.info("[SETTINGS] loaded %d override(s) from Neon: %s", len(overrides), sorted(overrides))
    return True


def save(db, key: str, raw: Any, updated_by: str | None) -> dict:
    """Validate, write ONE row, then update memory. The only write path (an admin pressing Save)."""
    from models.app_setting import AppSetting

    definition = DEFINITIONS.get(key)
    if definition is None:
        raise KeyError(key)
    value = parse(definition, raw)
    text = str(value)
    now = datetime.now(timezone.utc)
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=text, updated_by=updated_by, updated_at=now))
    else:
        row.value, row.updated_by, row.updated_at = text, updated_by, now
    db.commit()
    with _lock:
        _overrides[key] = text
        _meta[key] = {"updated_by": updated_by, "updated_at": now}
    return describe_one(key)


def clear(db, key: str) -> dict:
    """Remove an override (back to the environment value / default). One DELETE, only on an admin's click."""
    from models.app_setting import AppSetting

    if key not in DEFINITIONS:
        raise KeyError(key)
    row = db.get(AppSetting, key)
    if row is not None:
        db.delete(row)
        db.commit()
    with _lock:
        _overrides.pop(key, None)
        _meta.pop(key, None)
    return describe_one(key)


def describe_one(key: str) -> dict:
    definition = DEFINITIONS[key]
    low, high = _bounds(definition)
    overridden = key in _overrides
    meta = _meta.get(key, {})
    updated_at = meta.get("updated_at")
    return {
        "key": key,
        "label": definition.label,
        "description": definition.description,
        "type": "text" if definition.kind == "phone" else "number",
        "unit": definition.unit,
        "min": low,
        "max": high,
        "value": get(key),
        "source": "admin" if overridden else ("env" if os.environ.get(key) not in (None, "") else "default"),
        "fallback": _env_or_default(definition),
        "updated_by": meta.get("updated_by"),
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
    }


def describe() -> list[dict]:
    return [describe_one(key) for key in DEFINITIONS]


def reset() -> None:
    """Test helper: forget every override and the once-per-startup warnings."""
    with _lock:
        _overrides.clear()
        _meta.clear()
        _warned.clear()
