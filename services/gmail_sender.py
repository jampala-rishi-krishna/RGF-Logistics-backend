"""Send email directly through the Gmail API (no n8n in the path).

Uses the same dispatcher mailbox credentials as routers/gmail.py (GMAIL_COMMS_CLIENT_ID /
_CLIENT_SECRET / _REFRESH_TOKEN). The refresh token's `gmail.modify` scope includes sending.

Every attempt - success or failure - is logged and kept in a small in-memory ring buffer
(`recent()`), exposed at GET /api/gmail/send-log, so a failed send is never silent.
Nothing is persisted (no new tables), per the database discipline rules.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from html import unescape
from typing import Iterable, Sequence

import httpx

logger = logging.getLogger("gmail_sender")

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
MAX_RAW_BYTES = 20 * 1024 * 1024
_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

_token_lock = threading.Lock()
_token: dict = {"value": None, "expires_at": 0.0}
_log_lock = threading.Lock()
_log: deque = deque(maxlen=200)
_mailbox: dict = {"address": None}


class GmailSendError(Exception):
    """A send that did not happen. `status_code` is what the API should answer with."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def configured() -> bool:
    return all(os.environ.get(name, "").strip() for name in ("GMAIL_COMMS_CLIENT_ID", "GMAIL_COMMS_CLIENT_SECRET", "GMAIL_COMMS_REFRESH_TOKEN"))


def _google_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or f"HTTP {response.status_code}"
    error = body.get("error")
    if isinstance(error, dict):
        reasons = ",".join(str(item.get("reason")) for item in error.get("errors") or [] if item.get("reason"))
        return f"{error.get('message') or 'error'}{f' [{reasons}]' if reasons else ''}"
    return f"{error or 'error'}: {body.get('error_description') or ''}".strip(": ")


def _access_token(force: bool = False) -> str:
    with _token_lock:
        if not force and _token["value"] and time.time() < _token["expires_at"] - 60:
            return _token["value"]
    if not configured():
        raise GmailSendError("Gmail is not configured on the server (GMAIL_COMMS_CLIENT_ID / GMAIL_COMMS_CLIENT_SECRET / GMAIL_COMMS_REFRESH_TOKEN).", 503)
    try:
        response = httpx.post(
            TOKEN_URL,
            data={
                "client_id": os.environ["GMAIL_COMMS_CLIENT_ID"].strip(),
                "client_secret": os.environ["GMAIL_COMMS_CLIENT_SECRET"].strip(),
                "refresh_token": os.environ["GMAIL_COMMS_REFRESH_TOKEN"].strip(),
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise GmailSendError(f"Could not reach Google to authorize Gmail: {exc}", 502) from exc
    if response.status_code != 200:
        detail = _google_error(response)
        logger.error("[GMAIL_SEND] token refresh failed status=%s detail=%s", response.status_code, detail)
        raise GmailSendError(f"Gmail authorization failed ({detail}). The mailbox may need to be reconnected.", 502)
    payload = response.json()
    with _token_lock:
        _token.update(value=payload["access_token"], expires_at=time.time() + int(payload.get("expires_in", 3600)))
        return _token["value"]


def mailbox_address() -> str | None:
    """The authenticated mailbox (cached); used for the From header."""
    if _mailbox["address"]:
        return _mailbox["address"]
    try:
        response = httpx.get(PROFILE_URL, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=15)
        if response.status_code == 200:
            _mailbox["address"] = response.json().get("emailAddress")
    except (httpx.HTTPError, GmailSendError):
        pass
    return _mailbox["address"]


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h[1-6])>", "\n", text)
    text = unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _recipients(value: str | Iterable[str] | None, label: str) -> list[str]:
    items = [value] if isinstance(value, str) else list(value or [])
    cleaned = [part.strip() for item in items for part in re.split(r"[;,]", str(item or "")) if part.strip()]
    bad = [item for item in cleaned if not _EMAIL_RE.match(item)]
    if bad:
        raise GmailSendError(f"Invalid {label} email address: {', '.join(bad)}", 422)
    return list(dict.fromkeys(cleaned))


def build_raw_message(*, to: list[str], subject: str, html: str, text: str | None = None, cc: list[str] | None = None, attachments: Sequence[tuple[str, str, bytes]] = (), sender: str | None = None, reply_to: str | None = None) -> str:
    message = EmailMessage()
    from_name = os.environ.get("GMAIL_FROM_NAME", "RareChain Logistics").strip()
    if sender:
        message["From"] = formataddr((from_name, sender)) if from_name else sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if reply_to:
        message["Reply-To"] = reply_to
    message["Subject"] = re.sub(r"[\r\n]+", " ", subject).strip()
    message.set_content(text or _strip_html(html) or " ")
    message.add_alternative(html, subtype="html")
    for filename, mime_type, data in attachments:
        maintype, _, subtype = (mime_type or "application/octet-stream").partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _record(entry: dict) -> None:
    with _log_lock:
        _log.appendleft(entry)


def recent(limit: int = 100) -> list[dict]:
    with _log_lock:
        return list(_log)[:limit]


def send_email(*, to: str | Iterable[str], subject: str, html: str, text: str | None = None, cc: str | Iterable[str] | None = None, attachments: Sequence[tuple[str, str, bytes]] = (), purpose: str = "email") -> dict:
    """Blocking send. Returns {"id", "threadId", "to"}; raises GmailSendError on any failure
    (after logging it). Run it in a thread from async code."""
    started = time.monotonic()
    entry = {"at": datetime.now(timezone.utc).isoformat(), "purpose": purpose, "to": [], "subject": subject, "attachments": [name for name, _, _ in attachments], "ok": False}
    try:
        recipients = _recipients(to, "recipient")
        entry["to"] = recipients
        if not recipients:
            raise GmailSendError("No recipient email address.", 422)
        if not (subject or "").strip():
            raise GmailSendError("Subject is required.", 422)
        raw = build_raw_message(to=recipients, subject=subject, html=html, text=text, cc=_recipients(cc, "cc") or None, attachments=attachments, sender=mailbox_address())
        if len(raw) > MAX_RAW_BYTES:
            raise GmailSendError("The email (with attachments) is too large for Gmail to send.", 413)
        result = None
        for attempt in (1, 2):
            try:
                response = httpx.post(SEND_URL, headers={"Authorization": f"Bearer {_access_token(force=attempt == 2)}"}, json={"raw": raw}, timeout=45)
            except httpx.HTTPError as exc:
                raise GmailSendError(f"Could not reach Gmail: {exc}", 502) from exc
            if response.status_code == 401 and attempt == 1:
                continue  # stale access token - refresh once and retry
            if response.status_code >= 400:
                detail = _google_error(response)
                hint = " The Gmail daily sending limit may have been reached." if response.status_code == 429 or "limit" in detail.lower() else ""
                raise GmailSendError(f"Gmail rejected the message (HTTP {response.status_code}: {detail}).{hint}", 502)
            result = response.json()
            break
        if result is None:
            raise GmailSendError("Gmail did not accept the message.", 502)
        entry.update(ok=True, messageId=result.get("id"), threadId=result.get("threadId"), ms=int((time.monotonic() - started) * 1000))
        logger.info("[GMAIL_SEND] ok purpose=%s to=%s subject=%r attachments=%d message_id=%s ms=%d", purpose, ",".join(recipients), subject, len(attachments), result.get("id"), entry["ms"])
        return {"id": result.get("id"), "threadId": result.get("threadId"), "to": recipients}
    except GmailSendError as exc:
        entry.update(error=str(exc), ms=int((time.monotonic() - started) * 1000))
        logger.error("[GMAIL_SEND] FAILED purpose=%s to=%s subject=%r error=%s", purpose, ",".join(entry["to"]) or to, subject, exc)
        raise
    except Exception as exc:  # never lose a failure silently
        entry.update(error=f"{type(exc).__name__}: {exc}", ms=int((time.monotonic() - started) * 1000))
        logger.exception("[GMAIL_SEND] FAILED (unexpected) purpose=%s to=%s subject=%r", purpose, entry["to"] or to, subject)
        raise GmailSendError(f"Unexpected error while sending: {exc}", 500) from exc
    finally:
        _record(entry)
