"""Send email directly through the Gmail API (no n8n in the path).

Uses the same dispatcher mailbox credentials as routers/gmail.py (GMAIL_COMMS_CLIENT_ID /
_CLIENT_SECRET / _REFRESH_TOKEN). The refresh token's `gmail.modify` scope includes sending.

Sender identity (all optional): GMAIL_FROM_ADDRESS (must be a verified "Send mail as" alias of
the authenticated mailbox, otherwise Gmail silently rewrites it to the mailbox address),
GMAIL_FROM_NAME and GMAIL_REPLY_TO. Defaults: the authenticated mailbox, "RareChain Logistics",
no Reply-To.

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
SENDAS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/settings/sendAs"
SENDAS_TTL_SECONDS = 300
LABELS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/labels"
MODIFY_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}/modify"
THREAD_MODIFY_URL = "https://gmail.googleapis.com/gmail/v1/users/me/threads/{id}/modify"
IDENTITY_ENV = ("GMAIL_FROM_ADDRESS", "GMAIL_FROM_NAME", "GMAIL_REPLY_TO")
LABEL_TTL_SECONDS = 3600
LOGISTICS_LABEL = "Logistics"
LOGISTICS_SENT_LABEL = "Logistics/Sent"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
MAX_RAW_BYTES = 20 * 1024 * 1024
_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

_token_lock = threading.Lock()
_token: dict = {"value": None, "expires_at": 0.0}
_log_lock = threading.Lock()
_log: deque = deque(maxlen=200)
_mailbox: dict = {"address": None}
_sent_keys: dict[str, float] = {}
_sendas: dict = {"addresses": None, "fetched_at": 0.0}
_labels: dict = {"ids": None, "fetched_at": 0.0}
DEDUPE_WINDOW_SECONDS = 120


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


def send_as_addresses(force: bool = False) -> set[str] | None:
    """Lower-cased addresses the mailbox may legitimately send as (primary + verified Send-As
    aliases), cached for SENDAS_TTL_SECONDS. None when Gmail could not be asked (e.g. the token
    lacks the scope) - callers must treat that as "unknown", not "unavailable"."""
    with _token_lock:
        cached = _sendas["addresses"]
        if not force and cached is not None and time.time() - _sendas["fetched_at"] < SENDAS_TTL_SECONDS:
            return cached
    try:
        response = httpx.get(SENDAS_URL, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=15)
    except (httpx.HTTPError, GmailSendError):
        return None
    if response.status_code != 200:
        logger.warning("[GMAIL_SEND] could not list send-as aliases: HTTP %s", response.status_code)
        return None
    found = {
        str(item.get("sendAsEmail") or "").lower()
        for item in response.json().get("sendAs", [])
        if item.get("isPrimary") or item.get("verificationStatus") in (None, "accepted")
    }
    with _token_lock:
        _sendas.update(addresses=found, fetched_at=time.time())
    return found


def _resolve_labels() -> tuple[dict[str, str], dict[str, str]]:
    """Look the Logistics labels up BY NAME (never hardcoded ids), creating any that are missing.
    Returns ({name: id}, {name: error}); ids are cached for LABEL_TTL_SECONDS."""
    with _token_lock:
        cached = _labels["ids"]
        if cached is not None and time.time() - _labels["fetched_at"] < LABEL_TTL_SECONDS:
            return dict(cached), {}
    headers = {"Authorization": f"Bearer {_access_token()}"}
    response = httpx.get(LABELS_URL, headers=headers, timeout=15)
    if response.status_code != 200:
        raise GmailSendError(f"Could not list Gmail labels (HTTP {response.status_code}: {_google_error(response)})", 502)
    existing = {str(item.get("name") or "").lower(): item["id"] for item in response.json().get("labels", []) if item.get("id")}
    ids: dict[str, str] = {}
    errors: dict[str, str] = {}
    for name in (LOGISTICS_LABEL, LOGISTICS_SENT_LABEL):
        found = existing.get(name.lower())
        if not found:
            created = httpx.post(LABELS_URL, headers=headers, json={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}, timeout=15)
            if created.status_code != 200:
                errors[name] = f"could not create label (HTTP {created.status_code}: {_google_error(created)})"
                continue
            found = created.json().get("id")
            logger.info("[GMAIL_SEND] created missing Gmail label %r", name)
        ids[name] = found
    if not errors:
        with _token_lock:
            _labels.update(ids=dict(ids), fetched_at=time.time())
    return ids, errors


_label_lookup: dict = {"labels": None, "fetched_at": 0.0}
LABEL_LOOKUP_TTL_SECONDS = 60


def lookup_label_ids(names: Sequence[str]) -> dict[str, str]:
    """READ-ONLY: {name: id} for the Gmail labels that already exist (case-insensitive). Unlike
    _resolve_labels this never creates a label - used by the dashboard, which must only display."""
    with _token_lock:
        cached = _label_lookup["labels"]
        fresh = cached is not None and time.time() - _label_lookup["fetched_at"] < LABEL_LOOKUP_TTL_SECONDS
    if not fresh:
        response = httpx.get(LABELS_URL, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=15)
        if response.status_code != 200:
            raise GmailSendError(f"Could not list Gmail labels (HTTP {response.status_code}: {_google_error(response)})", 502)
        cached = {str(item.get("name") or "").lower(): item["id"] for item in response.json().get("labels", []) if item.get("id")}
        with _token_lock:
            _label_lookup.update(labels=cached, fetched_at=time.time())
    return {name: cached[name.lower()] for name in names if name.lower() in cached}


def _modify(url: str, add: list[str] | None = None, remove: list[str] | None = None) -> None:
    body: dict[str, list[str]] = {}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove
    response = httpx.post(url, headers={"Authorization": f"Bearer {_access_token()}"}, json=body, timeout=15)
    if response.status_code != 200:
        with _token_lock:
            _labels.update(ids=None, fetched_at=0.0)  # a deleted/renamed label must be re-resolved
        raise GmailSendError(f"Gmail rejected the label change (HTTP {response.status_code}: {_google_error(response)})", 502)


def remove_inbox_label(message_id: str, label_ids: Sequence[str] | None = None) -> dict:
    """Archive one processed inbound message by removing Gmail's INBOX system label.

    This is deliberately message-level, not thread-level: a logistics thread can include sent
    replies and team notifications that must keep their normal Gmail state. Never raises; archive
    failures must not cause the agent to retry or resend.
    """
    result = {"inbox": "skipped"}
    if not message_id:
        result["reason"] = "missing message id"
        return result
    if label_ids is not None and "INBOX" not in set(label_ids):
        result["reason"] = "INBOX already absent"
        return result
    try:
        _modify(MODIFY_URL.format(id=message_id), remove=["INBOX"])
        result["inbox"] = "removed"
    except Exception as exc:
        result.update(inbox="failed", error=str(exc)[:300])
        logger.error("[GMAIL_SEND] processed inbound message %s kept INBOX label: %s", message_id, result)
    return result


def apply_logistics_labels(message_id: str, thread_id: str | None = None) -> dict:
    """Add Logistics + Logistics/Sent to an already-sent message. Never raises: a labeling
    failure must not affect (or cause a resend of) the email. Returns per-label status.

    Without `thread_id` (a new conversation) both labels go on the message in one call. With it
    (a reply into an existing thread) `Logistics` goes on the whole THREAD, so the driver's inbound
    messages are labeled too, and `Logistics/Sent` stays on the sent message only."""
    result = {"logistics": "failed", "logisticsSent": "failed"}
    try:
        ids, errors = _resolve_labels()
        wanted = [(LOGISTICS_LABEL, "logistics"), (LOGISTICS_SENT_LABEL, "logisticsSent")]
        if thread_id:
            plan = [(THREAD_MODIFY_URL.format(id=thread_id), LOGISTICS_LABEL, "logistics"), (MODIFY_URL.format(id=message_id), LOGISTICS_SENT_LABEL, "logisticsSent")]
            for url, name, key in plan:
                if name not in ids:
                    continue
                try:
                    _modify(url, [ids[name]])
                    result[key] = "applied"
                except Exception as exc:
                    result["error"] = "; ".join(filter(None, [result.get("error"), f"{name}: {str(exc)[:200]}"]))
        else:
            add = [ids[name] for name, _ in wanted if name in ids]
            if add:
                _modify(MODIFY_URL.format(id=message_id), add)
                for name, key in wanted:
                    if name in ids:
                        result[key] = "applied"
        if errors:
            result["error"] = "; ".join(filter(None, [result.get("error"), *(f"{name}: {why}" for name, why in errors.items())]))
    except Exception as exc:  # label problems are logged, never raised
        result["error"] = str(exc)[:300]
    if result["logistics"] != "applied" or result["logisticsSent"] != "applied":
        logger.error("[GMAIL_SEND] email %s was SENT but labeling failed: %s", message_id, result)
    return result


def identity_config_problems() -> list[str]:
    """Names of the sender-identity variables that are blank. Production must set all three."""
    return [name for name in IDENTITY_ENV if not os.environ.get(name, "").strip()]


def is_production() -> bool:
    return bool(os.environ.get("RENDER")) or os.environ.get("APP_ENV", os.environ.get("ENVIRONMENT", "")).strip().lower() in {"production", "prod"}


def identity_health() -> dict:
    """Cheap, network-free summary for /health."""
    missing = identity_config_problems()
    return {"configured": configured(), "missingIdentityConfig": missing, "ok": configured() and not missing}


def log_identity_config_at_startup() -> None:
    """Fail loudly (ERROR) if production is missing any sender-identity variable, then verify
    the alias with Gmail in the background so a bad alias is also reported at boot."""
    missing = identity_config_problems()
    if missing and is_production():
        logger.error("[GMAIL_IDENTITY] PRODUCTION is missing %s: logistics email would go out as the raw mailbox identity. Set them in Render.", ", ".join(missing))
    elif missing:
        logger.warning("[GMAIL_IDENTITY] not set (non-production): %s", ", ".join(missing))
    if not configured():
        logger.error("[GMAIL_IDENTITY] GMAIL_COMMS_* credentials are not configured: no email can be sent.")
        return

    def check() -> None:
        try:
            status = identity_status()
            if not status.get("usable"):
                logger.error("[GMAIL_IDENTITY] %s", status.get("error"))
        except Exception as exc:
            logger.error("[GMAIL_IDENTITY] could not verify the sender identity: %s", exc)

    threading.Thread(target=check, daemon=True, name="gmail-identity-check").start()


def identity_status() -> dict:
    """Diagnostic for GET /api/gmail/identity. Never includes credentials."""
    if not configured():
        return {"configured": False, "usable": False, "error": "GMAIL_COMMS_* credentials are not set."}
    mailbox = mailbox_address()
    wanted = os.environ.get("GMAIL_FROM_ADDRESS", "").strip()
    aliases = send_as_addresses(force=True)
    available = None if aliases is None else (not wanted or wanted.lower() in aliases or wanted.lower() == (mailbox or "").lower())
    status = {
        "configured": True,
        "authenticatedMailbox": mailbox,
        "configuredFromAddress": wanted or None,
        "effectiveFromAddress": wanted or mailbox,
        "fromName": os.environ.get("GMAIL_FROM_NAME", "RareChain Logistics").strip(),
        "replyTo": os.environ.get("GMAIL_REPLY_TO", "").strip() or None,
        "sendAsAliasAvailable": available,
        "missingIdentityConfig": identity_config_problems(),
        "usable": available is not False,
    }
    if available is False:
        status["error"] = f"GMAIL_FROM_ADDRESS {wanted} is not a verified Send-As alias of {mailbox}. Emails will be refused until it is added in Gmail settings or the variable is cleared."
    elif available is None:
        status["warning"] = "Could not list Send-As aliases; the configured From address is unverified."
    return status


def authorized_headers() -> dict:
    """Bearer header for read-only Gmail calls made by other backend services."""
    return {"Authorization": f"Bearer {_access_token()}"}


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


def build_raw_message(*, to: list[str], subject: str, html: str, text: str | None = None, cc: list[str] | None = None, attachments: Sequence[tuple[str, str, bytes]] = (), sender: str | None = None, reply_to: str | None = None, from_name: str | None = None, in_reply_to: str | None = None, references: str | None = None, extra_headers: dict[str, str] | None = None) -> str:
    message = EmailMessage()
    from_name = (from_name if from_name is not None else os.environ.get("GMAIL_FROM_NAME", "RareChain Logistics")).strip()
    if sender:
        message["From"] = formataddr((from_name, sender)) if from_name else sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if reply_to:
        message["Reply-To"] = reply_to
    message["Subject"] = re.sub(r"[\r\n]+", " ", subject).strip()
    if in_reply_to:
        message["In-Reply-To"] = re.sub(r"[\r\n]+", " ", in_reply_to).strip()
        message["References"] = re.sub(r"[\r\n]+", " ", references or in_reply_to).strip()
    for header, value in (extra_headers or {}).items():
        message[header] = re.sub(r"[\r\n]+", " ", str(value)).strip()
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


def _already_sent(key: str | None) -> bool:
    if not key:
        return False
    now = time.time()
    with _log_lock:
        for stale in [k for k, at in _sent_keys.items() if now - at > DEDUPE_WINDOW_SECONDS]:
            del _sent_keys[stale]
        return key in _sent_keys


def send_email(*, to: str | Iterable[str], subject: str, html: str, text: str | None = None, cc: str | Iterable[str] | None = None, attachments: Sequence[tuple[str, str, bytes]] = (), purpose: str = "email", from_address: str | None = None, from_name: str | None = None, reply_to: str | None = None, dedupe_key: str | None = None, label_logistics: bool = True, thread_id: str | None = None, in_reply_to: str | None = None, references: str | None = None, extra_headers: dict[str, str] | None = None) -> dict:
    """Blocking send. Returns {"id", "threadId", "to"}; raises GmailSendError on any failure
    (after logging it). Run it in a thread from async code.

    `dedupe_key`: if the same key was sent successfully within DEDUPE_WINDOW_SECONDS the send is
    skipped and {"duplicate": True} is returned - guards against double-clicks / retried
    requests without blocking a deliberate resend later. Failed sends never block a retry."""
    started = time.monotonic()
    entry = {"at": datetime.now(timezone.utc).isoformat(), "purpose": purpose, "to": [], "subject": subject, "attachments": [name for name, _, _ in attachments], "ok": False}
    try:
        recipients = _recipients(to, "recipient")
        entry["to"] = recipients
        if not recipients:
            raise GmailSendError("No recipient email address.", 422)
        if not (subject or "").strip():
            raise GmailSendError("Subject is required.", 422)
        if _already_sent(dedupe_key):
            entry.update(ok=True, duplicate=True, ms=0)
            logger.warning("[GMAIL_SEND] skipped duplicate purpose=%s to=%s subject=%r", purpose, ",".join(recipients), subject)
            return {"id": None, "threadId": None, "to": recipients, "duplicate": True}
        sender = (from_address or os.environ.get("GMAIL_FROM_ADDRESS", "")).strip() or mailbox_address()
        own = (mailbox_address() or "").lower()
        if sender and own and sender.lower() != own:
            aliases = send_as_addresses()
            if aliases is not None and sender.lower() not in aliases:
                raise GmailSendError(f"From address {sender} is not a verified Gmail Send-As alias of {own}; refusing to send as an identity Gmail would silently rewrite. Add the alias in Gmail settings or clear GMAIL_FROM_ADDRESS.", 503)
        reply = (reply_to or os.environ.get("GMAIL_REPLY_TO", "")).strip() or None
        raw = build_raw_message(to=recipients, subject=subject, html=html, text=text, cc=_recipients(cc, "cc") or None, attachments=attachments, sender=sender, reply_to=reply, from_name=from_name, in_reply_to=in_reply_to, references=references, extra_headers=extra_headers)
        entry.update(sender=sender, replyTo=reply)
        if len(raw) > MAX_RAW_BYTES:
            raise GmailSendError("The email (with attachments) is too large for Gmail to send.", 413)
        result = None
        for attempt in (1, 2):
            try:
                response = httpx.post(SEND_URL, headers={"Authorization": f"Bearer {_access_token(force=attempt == 2)}"}, json={"raw": raw, **({"threadId": thread_id} if thread_id else {})}, timeout=45)
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
        if dedupe_key:
            with _log_lock:
                _sent_keys[dedupe_key] = time.time()
        if label_logistics and result.get("id"):
            entry["labels"] = apply_logistics_labels(result["id"], thread_id=thread_id)
        logger.info("[GMAIL_SEND] ok purpose=%s to=%s subject=%r attachments=%d message_id=%s ms=%d", purpose, ",".join(recipients), subject, len(attachments), result.get("id"), entry["ms"])
        return {"id": result.get("id"), "threadId": result.get("threadId"), "to": recipients, "labels": entry.get("labels")}
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
