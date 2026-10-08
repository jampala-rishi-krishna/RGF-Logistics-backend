"""Weekly eco-driving scorecard to drivers over WhatsApp. Admin switch, DEFAULT OFF.

The switch lives in memory (no Neon table): a restart always returns it to OFF, so it can never turn itself on. Sending goes through
the existing WhatsApp path (/api/dispatch/send), so the WhatsApp pause switch is respected as well. Drivers without a phone number
and drivers without enough data are skipped and shown as such in the preview. Monday 08:00 Asia/Manila.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from services import staff_directory_cache, whatsapp_control
from services.fleet_health import config, eco, eco_views, snapshot

logger = logging.getLogger("fleet_health.scorecard")

_lock = threading.Lock()
_state = {"enabled": False, "changed_by": None, "changed_at": None}


def status() -> dict:
    with _lock:
        return {**_state, "default": False, "schedule": "Monday 08:00 Asia/Manila", "whatsapp_active": whatsapp_control.is_active()}


def set_enabled(enabled: bool, actor: str | None) -> dict:
    with _lock:
        _state.update(enabled=bool(enabled), changed_by=actor or "unknown", changed_at=datetime.now(timezone.utc).isoformat())
    logger.info("[SCORECARD] weekly WhatsApp scorecard %s by %s", "ENABLED" if enabled else "DISABLED", actor)
    return status()


def reset() -> None:
    with _lock:
        _state.update(enabled=False, changed_by=None, changed_at=None)


def _phone(staff_id) -> str | None:
    member = staff_directory_cache.get_by_id(staff_id, retry_on_miss=False) if staff_id is not None else None
    return (member or {}).get("phone") or None


def preview(db: Session, week: date | None = None) -> dict:
    """What would be sent for `week` (default: the last full Mon-Sun week), per driver. Sends nothing."""
    ctx = snapshot.load_context(db, only={"daily", "fuel"})
    start = eco.week_start(week) if week else snapshot.last_full_week(ctx.today)
    plates = {v["id"]: v["plate"] for v in ctx.vehicles}
    items = []
    for driver in eco_views.driver_scores(ctx.daily, ctx.fuel, start, plate_of=plates):
        message = eco_views.scorecard_message(driver)
        has_phone = bool(_phone(driver["staff_id"]))
        skip = message["skip_reason"] or (None if has_phone else "No phone number on file")
        items.append({"staff_id": driver["staff_id"], "name": driver["name"], "score": driver["score"], "trucks": driver["trucks"], "message": message["text"], "has_phone": has_phone, "will_send": skip is None, "skip_reason": skip})
    return {"week_start": start.isoformat(), "week_end": (start.fromordinal(start.toordinal() + 6)).isoformat(), "items": items, "will_send_count": sum(1 for i in items if i["will_send"])}


async def send_weekly(db: Session) -> dict:
    """The Monday job. Does nothing unless the scorecard switch is ON and WhatsApp itself is not paused."""
    if not status()["enabled"]:
        return {"sent": 0, "reason": "scorecard switch is off"}
    if not whatsapp_control.is_active():
        logger.info("[SCORECARD] skipped: WhatsApp messages are paused")
        return {"sent": 0, "reason": "WhatsApp is paused"}
    from routers import dispatch

    plan = preview(db)
    sent = 0
    for item in plan["items"]:
        if not item["will_send"]:
            continue
        try:
            await dispatch.send_message(dispatch.SendMessageBody(audience="driver", recipient_id=item["staff_id"], channels=["whatsapp"], body=item["message"], trigger_event="eco_scorecard"))
            sent += 1
        except Exception as exc:  # noqa: BLE001 - one driver's failure must not stop the others
            logger.warning("[SCORECARD] not sent to staff %s: %s", item["staff_id"], exc)
    logger.info("[SCORECARD] week %s: %d sent, %d skipped", plan["week_start"], sent, len(plan["items"]) - sent)
    return {"sent": sent, "skipped": len(plan["items"]) - sent, "week_start": plan["week_start"]}
