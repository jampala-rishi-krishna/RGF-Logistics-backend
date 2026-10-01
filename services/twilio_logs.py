"""Read-only Twilio WhatsApp delivery logs for the IntelliFleet logistics assignment template.

Only reads Twilio's Messages API. Results are cached in memory for 60 s per `since` date
(no Neon reads or writes). Other templates on the same sender belong to a different project
and are filtered out client-side, because the Messages API cannot filter by ContentSid.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger("twilio_logs")

API_ROOT = "https://api.twilio.com"
CACHE_TTL_SECONDS = 60
PAGE_SIZE = 200
MAX_PAGES = 20  # hard stop so a bad cursor can never loop forever
TEMPLATE_PREFIX = "hi "
TEMPLATE_MARKER = "you've been assigned truck"  # rendered body of ContentSid HXb087f0084bcd371f016905794aa89d72

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_key_locks: dict[str, threading.Lock] = {}


class TwilioNotConfigured(Exception):
    pass


class TwilioError(Exception):
    pass


def _config() -> tuple[str, str, str, str]:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    key_sid = os.environ.get("TWILIO_API_KEY_SID", "").strip()
    key_secret = os.environ.get("TWILIO_API_KEY_SECRET", "").strip()
    sender = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+639171145694").strip()
    missing = [name for name, value in (("TWILIO_ACCOUNT_SID", account_sid), ("TWILIO_API_KEY_SID", key_sid), ("TWILIO_API_KEY_SECRET", key_secret)) if not value]
    if missing:
        raise TwilioNotConfigured("Twilio is not configured: missing " + ", ".join(missing))
    return account_sid, key_sid, key_secret, sender


def is_logistics_template(message: dict) -> bool:
    """Outbound API message whose rendered body is the logistics truck-assignment template."""
    if message.get("direction") != "outbound-api":
        return False
    body = str(message.get("body") or "").replace("’", "'")
    lowered = body.lower()
    return lowered.startswith(TEMPLATE_PREFIX) and TEMPLATE_MARKER in lowered


def _phone(value: str | None) -> str:
    return str(value or "").replace("whatsapp:", "").strip()


def _fetch_all(client: httpx.Client, account_sid: str, params: dict) -> list[dict]:
    url = f"{API_ROOT}/2010-04-01/Accounts/{account_sid}/Messages.json"
    messages: list[dict] = []
    next_params: dict | None = params
    for _ in range(MAX_PAGES):
        try:
            response = client.get(url, params=next_params)
        except httpx.HTTPError as exc:
            raise TwilioError("Twilio is unreachable.") from exc
        if response.status_code in {401, 403}:
            raise TwilioError("Twilio rejected the credentials.")
        if response.status_code != 200:
            raise TwilioError(f"Twilio returned HTTP {response.status_code}.")
        body = response.json()
        messages.extend(body.get("messages") or [])
        next_uri = body.get("next_page_uri")
        if not next_uri:
            return messages
        url, next_params = f"{API_ROOT}{next_uri}", None  # the cursor URI already carries every parameter
    logger.warning("[TWILIO_LOGS] stopped after %s pages", MAX_PAGES)
    return messages


def _iso(value: str | None) -> str | None:
    """Twilio returns RFC 2822 dates; normalize to ISO 8601 UTC so they sort and parse everywhere."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return value


def _outbound_row(message: dict) -> dict:
    price = message.get("price")
    return {
        "sid": message.get("sid"),
        "date_created": _iso(message.get("date_created")),
        "date_sent": _iso(message.get("date_sent")),
        "to": _phone(message.get("to")),
        "status": message.get("status"),
        "error_code": message.get("error_code"),
        "error_message": message.get("error_message"),
        "price": f"{price} {message.get('price_unit') or ''}".strip() if price is not None else None,
        "body": message.get("body"),
    }


def build_logs(since: str) -> dict:
    account_sid, key_sid, key_secret, sender = _config()
    with httpx.Client(auth=(key_sid, key_secret), timeout=30) as client:
        sent = _fetch_all(client, account_sid, {"From": sender, "DateSent>": since, "PageSize": PAGE_SIZE})
        outbound = [_outbound_row(m) for m in sent if is_logistics_template(m)]
        recipients = {row["to"] for row in outbound}
        received = _fetch_all(client, account_sid, {"To": sender, "DateSent>": since, "PageSize": PAGE_SIZE})
    inbound = [
        {"sid": m.get("sid"), "from": _phone(m.get("from")), "date": _iso(m.get("date_sent") or m.get("date_created")), "body": m.get("body")}
        for m in received
        if str(m.get("direction") or "").startswith("inbound") and _phone(m.get("from")) in recipients
    ]
    return {
        "since": since,
        "from": _phone(sender),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "outbound": sorted(outbound, key=lambda r: r["date_sent"] or r["date_created"] or ""),
        "inbound": sorted(inbound, key=lambda r: r["date"] or ""),
    }


def get_logs(since: str) -> dict:
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(since)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return {**cached[1], "cached": True}
        lock = _key_locks.setdefault(since, threading.Lock())
    with lock:  # one caller refreshes; concurrent callers wait and reuse the result
        with _cache_lock:
            cached = _cache.get(since)
            if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
                return {**cached[1], "cached": True}
        result = build_logs(since)  # failures are never cached
        with _cache_lock:
            _cache[since] = (time.monotonic(), result)
        return {**result, "cached": False}
