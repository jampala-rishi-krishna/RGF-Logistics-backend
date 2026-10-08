"""Master switch for outbound AI voice calls (Vapi/Twilio), "active" | "paused".

DEFAULT IS PAUSED. Anything that is not exactly "active" (unset, typo, garbage) resolves to paused.

Persistence (no Neon table/column, per docs/DATABASE.md discipline): the startup state comes from env
VOICE_CALLS_DEFAULT; an admin can flip it at runtime (held in memory); a restart returns to the env
value, so it can never silently come back "active" unless the env says so. One Render instance only.

Paused means every outbound call is skipped BEFORE Vapi/Twilio is contacted. Nothing is queued and
nothing is replayed on resume. Calls already in progress are never cut off.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger("voice_control")

ACTIVE = "active"
PAUSED = "paused"

_lock = threading.Lock()


def normalize(value) -> str:
    """"active" (any case/padding) -> active; everything else -> paused."""
    return ACTIVE if str(value or "").strip().lower() == ACTIVE else PAUSED


def default_mode() -> str:
    return normalize(os.environ.get("VOICE_CALLS_DEFAULT"))


_state: dict = {"mode": default_mode(), "changed_by": None, "changed_at": None}


class VoiceCallsPaused(Exception):
    """Raised by the lowest-level call creator as a last line of defence."""


def mode() -> str:
    with _lock:
        return _state["mode"]


def is_active() -> bool:
    return mode() == ACTIVE


def status() -> dict:
    with _lock:
        return {"voice_calls": _state["mode"], "changed_by": _state["changed_by"], "changed_at": _state["changed_at"], "default": default_mode()}


def set_mode(new_mode: str, *, actor_name: str | None, actor_id=None) -> dict:
    """Admin toggle. Only exactly "active" / "paused" are accepted (anything else raises ValueError)."""
    cleaned = str(new_mode or "").strip().lower()
    if cleaned not in (ACTIVE, PAUSED):
        raise ValueError('voice_calls must be "active" or "paused"')
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        previous = _state["mode"]
        _state.update(mode=cleaned, changed_by=actor_name or "unknown", changed_at=now)
    logger.info("[VOICE] AI voice calls %s by %s at %s (was %s)", "RESUMED" if cleaned == ACTIVE else "PAUSED", actor_name or "unknown", now, previous)
    try:
        from services.audit import write_audit_log

        write_audit_log(None, actor_id=str(actor_id) if actor_id is not None else None, action=f"voice_calls_{'resumed' if cleaned == ACTIVE else 'paused'}", target_entity="voice_calls", target_id="global", details={"from": previous, "to": cleaned, "by": actor_name})
    except Exception:  # auditing must never block the switch
        logger.warning("[VOICE] audit entry could not be written", exc_info=True)
    return status()


def reset_to_default() -> None:
    """What a process restart does: back to the env value (tests call this too)."""
    with _lock:
        _state.update(mode=default_mode(), changed_by=None, changed_at=None)


def log_skipped(*, so_numbers=None, driver: str | None = None, what: str = "driver call") -> None:
    """INFO line for a skipped call. Never includes phone numbers."""
    logger.info("[VOICE] skipped: calls paused (%s) so=%s driver=%s", what, ",".join(str(s) for s in (so_numbers or []) if s) or "-", driver or "-")
