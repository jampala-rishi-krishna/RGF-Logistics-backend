from __future__ import annotations

import base64
import logging
import os
import time as time_module
from datetime import date, datetime, time, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException

from auth.dependencies import require_role

logger = logging.getLogger("gmail")

router = APIRouter(prefix="/api/gmail", tags=["gmail"], dependencies=[Depends(require_role("admin", "dispatcher", "warehouse"))])

GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
MANILA_TZ = ZoneInfo("Asia/Manila")
GMAIL_ENV = {
    "client_id": "GMAIL_COMMS_CLIENT_ID",
    "client_secret": "GMAIL_COMMS_CLIENT_SECRET",
    "refresh_token": "GMAIL_COMMS_REFRESH_TOKEN",
}

# Cached in-process; refreshed lazily whenever it's within 60s of expiry. A single dispatcher
# operations mailbox is connected here (not per-user OAuth) so no token table/DB model is needed.
_token_cache: dict = {"access_token": None, "expires_at": 0}


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise HTTPException(503, f"{name} is not configured on the server")
    return value


async def _get_access_token() -> str:
    now = time_module.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    client_id = _require_env(GMAIL_ENV["client_id"])
    client_secret = _require_env(GMAIL_ENV["client_secret"])
    refresh_token = _require_env(GMAIL_ENV["refresh_token"])

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            GMAIL_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if resp.status_code != 200:
        logger.error("Gmail token refresh failed: %s", resp.status_code)
        raise HTTPException(502, "Could not refresh Gmail access token")

    payload = resp.json()
    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"] = now + int(payload.get("expires_in", 3600))
    return _token_cache["access_token"]


def _header(name: str, headers: list[dict]) -> str | None:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


def _decode_body(payload: dict) -> str:
    def walk(part: dict) -> str | None:
        mime_type = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")
        if body_data and mime_type in ("text/plain", "text/html"):
            try:
                decoded = base64.urlsafe_b64decode(body_data + "=" * (-len(body_data) % 4)).decode("utf-8", errors="replace")
            except Exception:
                return None
            return decoded
        for sub in part.get("parts", []) or []:
            found = walk(sub)
            if found:
                return found
        return None

    return walk(payload) or ""


def _decode_mime_body(payload: dict, mime_type: str) -> str:
    """Decode one MIME representation while preserving the original HTML when available."""
    body_data = payload.get("body", {}).get("data")
    if body_data and payload.get("mimeType") == mime_type:
        try:
            return base64.urlsafe_b64decode(body_data + "=" * (-len(body_data) % 4)).decode("utf-8", errors="replace")
        except Exception:
            return ""
    for part in payload.get("parts", []) or []:
        found = _decode_mime_body(part, mime_type)
        if found:
            return found
    return ""


def _strip_html(text: str) -> str:
    import re

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _today_query() -> str:
    today = datetime.now(MANILA_TZ).date()
    start = datetime.combine(today, time.min, tzinfo=MANILA_TZ).astimezone(timezone.utc)
    end = datetime.combine(today, time.max, tzinfo=MANILA_TZ).astimezone(timezone.utc)
    # Gmail's date operators use a mailbox-local interpretation. Unix seconds
    # make the Asia/Manila day boundary explicit and include both INBOX and SENT.
    return f"after:{int(start.timestamp())} before:{int(end.timestamp())}"


async def _fetch_message(client: httpx.AsyncClient, access_token: str, message_id: str) -> dict:
    resp = await client.get(
        f"{GMAIL_API_BASE}/messages/{message_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"format": "full"},
    )
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("payload", {})
    headers = payload.get("headers", [])
    date_header = _header("Date", headers)
    try:
        sent_at = parsedate_to_datetime(date_header).isoformat() if date_header else None
    except Exception:
        sent_at = None

    body_html = _decode_mime_body(payload, "text/html")
    body = _decode_body(payload)
    if "<" in body and ">" in body:
        body = _strip_html(body)

    label_ids = data.get("labelIds", []) or []
    return {
        "id": data.get("id"),
        "thread_id": data.get("threadId"),
        "from": _header("From", headers),
        "to": _header("To", headers),
        "subject": _header("Subject", headers),
        "snippet": data.get("snippet"),
        "body": body,
        "body_html": body_html,
        "sent_at": sent_at,
        "is_sent": "SENT" in label_ids,
        "is_unread": "UNREAD" in label_ids,
    }


@router.get("/messages/today")
async def list_today_messages(selected_date: date | None = None):
    access_token = await _get_access_token()
    mailbox_date = selected_date or datetime.now(MANILA_TZ).date()
    start = datetime.combine(mailbox_date, time.min, tzinfo=MANILA_TZ).astimezone(timezone.utc)
    end = datetime.combine(mailbox_date, time.max, tzinfo=MANILA_TZ).astimezone(timezone.utc)
    query = f"after:{int(start.timestamp())} before:{int(end.timestamp())}"

    async with httpx.AsyncClient(timeout=20) as client:
        list_resp = await client.get(
            f"{GMAIL_API_BASE}/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            # No labelIds filter: passing multiple label ids ANDs them (message must have
            # every label at once), which is never true for INBOX+SENT together and silently
            # returned zero results. `q`'s after:/before: already covers both directions.
            params={"q": query, "maxResults": 100},
        )
        if list_resp.status_code != 200:
            logger.error("Gmail message list failed: %s %s", list_resp.status_code, list_resp.text[:300])
            raise HTTPException(502, "Could not list Gmail messages")

        list_payload = list_resp.json()
        ids = [m["id"] for m in list_payload.get("messages", [])]
        logger.info(
            "Gmail today query=%s | resultSizeEstimate=%s | message_count=%s",
            query,
            list_payload.get("resultSizeEstimate"),
            len(ids),
        )
        messages = []
        for message_id in ids:
            try:
                messages.append(await _fetch_message(client, access_token, message_id))
            except httpx.HTTPStatusError:
                continue

    threads: dict[str, dict] = {}
    for m in messages:
        t = threads.setdefault(m["thread_id"], {"thread_id": m["thread_id"], "subject": m["subject"], "messages": []})
        t["messages"].append(m)

    result = []
    for t in threads.values():
        t["messages"].sort(key=lambda m: m["sent_at"] or "")
        latest = t["messages"][-1]
        participants = {m["from"] for m in t["messages"] if m["from"]} | {m["to"] for m in t["messages"] if m["to"]}
        result.append({
            "thread_id": t["thread_id"],
            "subject": t["subject"] or latest.get("subject") or "(no subject)",
            "participants": sorted(p for p in participants if p),
            "message_count": len(t["messages"]),
            "last_message_at": latest["sent_at"],
            "has_unread": any(m["is_unread"] for m in t["messages"]),
            "messages": t["messages"],
        })

    result.sort(key=lambda t: t["last_message_at"] or "", reverse=True)
    return {"date": mailbox_date.isoformat(), "threads": result}


@router.get("/status")
async def gmail_status():
    configured = all(os.environ.get(name, "").strip() for name in GMAIL_ENV.values())
    if not configured:
        return {"connected": False}
    try:
        await _get_access_token()
        return {"connected": True}
    except HTTPException:
        return {"connected": False}


@router.get("/profile")
async def gmail_profile():
    """Return the mailbox identity and totals for the configured OAuth token."""
    access_token = await _get_access_token()

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/profile",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code != 200:
        logger.error("Gmail profile failed: %s %s", resp.status_code, resp.text[:300])
        raise HTTPException(502, "Could not get Gmail profile")

    return resp.json()
