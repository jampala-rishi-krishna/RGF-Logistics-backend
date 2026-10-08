"""Master switch for outbound WhatsApp messages to drivers/staff, "active" | "paused".

Controlled ONLY from the admin panel (PUT /api/admin/whatsapp): no environment variable is needed.
The state lives in memory (no Neon table). A restart returns to DEFAULT_MODE below - change that one
constant if the startup state should be different.

Paused means every outbound WhatsApp send is skipped BEFORE n8n/Twilio is contacted. Nothing is
queued and nothing is replayed on resume. Email, SMS and voice are unaffected.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger("whatsapp_control")

ACTIVE = "active"
PAUSED = "paused"
DEFAULT_MODE = PAUSED  # startup state after every deploy/restart

_lock = threading.Lock()
_state: dict = {"mode": DEFAULT_MODE, "changed_by": None, "changed_at": None}


def mode() -> str:
    with _lock:
        return _state["mode"]


def is_active() -> bool:
    return mode() == ACTIVE


def status() -> dict:
    with _lock:
        return {"whatsapp": _state["mode"], "changed_by": _state["changed_by"], "changed_at": _state["changed_at"], "default": DEFAULT_MODE}


def set_mode(new_mode: str, *, actor_name: str | None, actor_id=None) -> dict:
    cleaned = str(new_mode or "").strip().lower()
    if cleaned not in (ACTIVE, PAUSED):
        raise ValueError('whatsapp must be "active" or "paused"')
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        previous = _state["mode"]
        _state.update(mode=cleaned, changed_by=actor_name or "unknown", changed_at=now)
    logger.info("[WHATSAPP] messages %s by %s at %s (was %s)", "RESUMED" if cleaned == ACTIVE else "PAUSED", actor_name or "unknown", now, previous)
    try:
        from services.audit import write_audit_log

        write_audit_log(None, actor_id=str(actor_id) if actor_id is not None else None, action=f"whatsapp_{'resumed' if cleaned == ACTIVE else 'paused'}", target_entity="whatsapp", target_id="global", details={"from": previous, "to": cleaned, "by": actor_name})
    except Exception:  # auditing must never block the switch
        logger.warning("[WHATSAPP] audit entry could not be written", exc_info=True)
    return status()


def reset_to_default() -> None:
    """What a process restart does (tests call this too)."""
    with _lock:
        _state.update(mode=DEFAULT_MODE, changed_by=None, changed_at=None)


def log_skipped(*, so_numbers=None, recipient: str | None = None, what: str = "assignment message") -> None:
    """INFO line for a skipped send. Never includes phone numbers."""
    logger.info("[WHATSAPP] skipped: messages paused (%s) so=%s recipient=%s", what, ",".join(str(s) for s in (so_numbers or []) if s) or "-", recipient or "-")
