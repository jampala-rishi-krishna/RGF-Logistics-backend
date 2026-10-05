"""Logistics email conversations, built read-only from Gmail threads (no n8n, no table).

Replaces the n8n "[LOGISTICS] Get Email Conversations" feed. Same shape the Comms views
already consume: {"count", "conversations": [{channel, contact, driverName, truckPlate,
warehouse, soNumbers, conversationStage, assignmentConfirmed, humanEscalated, lastUpdated,
lastMessageAt, messageCount, messages: [{role, content, ts}]}]}.

A thread is a driver conversation when it carries a reply recorded by the logistics agent, is
a "Driver assignment" email thread, or has a message from a Staff Directory member. System mail
that merely carries the Logistics label (team notifications, sales-order emails, tests) is left out.
Results are cached in memory for CACHE_SECONDS.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parseaddr

from services import gmail_sender, logistics_email_agent as agent, staff_directory_cache

logger = logging.getLogger("email_conversations")

CACHE_SECONDS = 60
LOOKBACK_QUERY = "label:logistics newer_than:30d"
MAX_THREADS = 50

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}
_SO = re.compile(r"\bSO[-\s]?\d{3,}[\w-]*", re.I)
_ASSIGNMENT_SUBJECT = re.compile(r"^(?:\s*(?:re|fwd?):\s*)*driver assignment\W+([A-Za-z0-9]+)", re.I)


def invalidate() -> None:
    with _lock:
        _cache.update(at=0.0, data=None)


def _iso(message: dict) -> str | None:
    millis = message.get("internalDate")
    return datetime.fromtimestamp(int(millis) / 1000, tz=timezone.utc).isoformat() if millis else None


def _counterpart(messages: list[dict]) -> tuple[str, str]:
    """(display name, email) of the driver side of the thread."""
    for message in messages:
        if not agent.is_own(message) and not agent.is_system(message):
            return agent._sender(message)
    own = [m for m in messages if agent.is_own(m)]
    for message in own:
        name, address = parseaddr(agent._headers(message).get("to", ""))
        if address and address.lower() not in agent._own_addresses():
            return name, address.lower()
    return "", ""


def build_conversation(thread: dict) -> dict | None:
    messages = sorted(thread.get("messages") or [], key=lambda m: int(m.get("internalDate") or 0))
    if not messages:
        return None
    subject = next((agent._headers(m).get("subject", "") for m in messages if agent._headers(m).get("subject")), "")
    assignment = _ASSIGNMENT_SUBJECT.match(subject)
    has_agent_reply = any(agent._headers(m).get(agent.ACTION_HEADER.lower()) for m in messages)
    external_senders = [agent._sender(m)[1] for m in messages if not agent.is_own(m) and not agent.is_system(m)]
    staff_sender = next((s for s in (agent._is_staff_match(a) for a in external_senders) if s), None)
    if not (assignment or has_agent_reply or staff_sender):
        return None
    display, contact = _counterpart(messages)
    staff = staff_sender or (agent._is_staff_match(contact) if contact else None)

    own_text = " ".join(agent.clean_body(m) for m in messages if agent.is_own(m))
    so_numbers = list(dict.fromkeys(re.sub(r"\s+", "", token).upper() for token in _SO.findall(own_text)))
    plate = assignment.group(1).upper() if assignment else None
    warehouse = {"METS": "Mets Cold Storage", "GLACIER": "Glacier Cold Storage"}.get(str((staff or {}).get("warehouse") or "").upper(), (staff or {}).get("warehouse"))
    if staff and not (plate and so_numbers):
        try:  # fall back to the live assignment when the thread text does not carry them
            live = agent.build_context(staff)
            plate = plate or (live["truckPlate"] or None)
            so_numbers = so_numbers or [o["soNumber"] for o in live["salesOrders"]]
        except Exception:
            logger.debug("live assignment lookup failed for %s", contact, exc_info=True)

    state = agent.rebuild_state(messages)
    rows = [{"role": "agent" if agent.is_own(m) else "driver", "content": agent.clean_body(m), "ts": _iso(m)} for m in messages]
    rows = [row for row in rows if row["content"]]
    last = rows[-1]["ts"] if rows else _iso(messages[-1])
    return {
        "channel": "email",
        "threadId": thread.get("id"),
        "contact": contact,
        "driverName": (staff or {}).get("name") or display or contact,
        "truckPlate": plate,
        "warehouse": warehouse,
        "soNumbers": so_numbers,
        "conversationStage": state["stage"],
        "assignmentConfirmed": state["confirmed"],
        "humanEscalated": state["escalated"],
        "lastUpdated": last,
        "lastMessageAt": last,
        "messageCount": len(rows),
        "messages": rows,
    }


def _fetch() -> dict:
    listing = agent._gmail_get("threads", {"q": LOOKBACK_QUERY, "maxResults": MAX_THREADS})
    ids = [t["id"] for t in listing.get("threads", [])]
    with ThreadPoolExecutor(max_workers=5) as pool:
        threads = list(pool.map(lambda tid: agent._gmail_get(f"threads/{tid}", {"format": "full"}), ids))
    conversations = [c for c in (build_conversation(t) for t in threads) if c]
    conversations.sort(key=lambda c: c.get("lastUpdated") or "", reverse=True)
    return {"count": len(conversations), "conversations": conversations}


def feed(*, tolerate_errors: bool = False) -> dict:
    """Cached (60s) conversations. With tolerate_errors, a Gmail problem yields an empty feed with
    an `error` field instead of raising (what comms-overview does for every channel)."""
    with _lock:
        if _cache["data"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]
    try:
        if not gmail_sender.configured():
            raise gmail_sender.GmailSendError("Gmail is not configured on the server.", 503)
        data = _fetch()
    except Exception as exc:
        logger.error("email conversations unavailable: %s", exc)
        if tolerate_errors:
            return {"count": 0, "conversations": [], "error": "Feed unavailable"}
        raise
    with _lock:
        _cache.update(at=time.monotonic(), data=data)
    return data
