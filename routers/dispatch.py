from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import time
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.dependencies import require_role
from services import memory_tables, staff_directory_cache, vapi_client, voice_calls

logger = logging.getLogger("dispatch")

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"], dependencies=[Depends(require_role("admin", "dispatcher", "warehouse"))])
_n8n_cache: dict[str, tuple[float, dict]] = {}
_N8N_FEEDS = {
    "email": "https://rareglobalfood.app.n8n.cloud/webhook/logistics-email-conversations",
    "whatsapp": "https://rareglobalfood.app.n8n.cloud/webhook/logistics-whatsapp-conversations",
    "sms": "https://rareglobalfood.app.n8n.cloud/webhook/logistics-sms-conversations",
}
_CHANNELS = (*_N8N_FEEDS, "voice")


def _n8n_conversations(channel: str, tolerate_errors: bool = False) -> dict:
    if channel == "voice":
        # Read from Vapi's own call log, whichever VOICE_PROVIDER places the calls - the n8n
        # voice DataTable is not read. Same response shape as the old n8n feed.
        try:
            return asyncio.run(voice_calls.conversations_feed())
        except vapi_client.VapiError as exc:
            logger.error("Vapi voice feed failed: %s", exc)
            if tolerate_errors:
                return {"count": 0, "conversations": [], "error": "Feed unavailable"}
            raise HTTPException(502, "The voice conversation feed is unavailable.") from exc
    now = time.monotonic()
    cached = _n8n_cache.get(channel)
    if cached and now - cached[0] < 20:
        return cached[1]
    headers = {}
    secret = os.environ.get("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET", "").strip()
    if secret:
        headers["Authorization"] = secret
    try:
        response = httpx.get(_N8N_FEEDS[channel], headers=headers, timeout=20)
        response.raise_for_status()
        body = response.json()
        if isinstance(body, list):
            body = {"count": len(body), "conversations": body}
        if not isinstance(body, dict):
            body = {"count": 0, "conversations": []}
        body.setdefault("conversations", [])
        body.setdefault("count", len(body["conversations"]))
        _n8n_cache[channel] = (now, body)
        return body
    except Exception as exc:
        logger.error("n8n conversation feed failed channel=%s error=%s", channel, exc)
        if tolerate_errors:
            return {"count": 0, "conversations": [], "error": "Feed unavailable"}
        raise HTTPException(502, f"The {channel} conversation feed is unavailable.") from exc


@router.get("/n8n-conversations/{channel}")
def n8n_conversations(channel: str):
    if channel not in _CHANNELS:
        raise HTTPException(404, "Unknown conversation channel.")
    return _n8n_conversations(channel)


@router.get("/voice-call-detail/{call_id}")
def voice_call_detail(call_id: str):
    try:
        return voice_calls.to_call_detail(asyncio.run(vapi_client.get_call(call_id)))
    except vapi_client.VapiError as exc:
        raise HTTPException(404 if exc.status_code == 404 else 502, "The voice call detail is unavailable.") from exc


@router.get("/voice-recording/{call_id}")
async def voice_recording(call_id: str):
    from routers.voice import recording_response
    return await recording_response(call_id)


def _overview_metrics(channel: str, conversations: list[dict], month_start) -> dict:
    active = []
    reply_times = []
    attention = 0
    confirmed = 0
    for conversation in conversations:
        stamp = conversation.get("lastUpdated") or conversation.get("lastMessageAt")
        try:
            local_day = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Manila")).date() if stamp else None
        except ValueError:
            local_day = None
        if local_day is None or local_day.replace(day=1) != month_start:
            continue
        active.append(conversation)
        if conversation.get("humanEscalated") or str(conversation.get("conversationStage") or "").upper() == "ISSUE_REPORTED":
            attention += 1
        if conversation.get("assignmentConfirmed") is True:
            confirmed += 1
        messages = conversation.get("messages") or []
        voice_connected = str(conversation.get("callStatus") or conversation.get("status") or "").lower() in {"connected", "in-progress", "in_progress", "ended", "completed"} and len(messages) > 1
        has_reply = voice_connected if channel == "voice" else str(conversation.get("conversationStage") or "").upper() != "ASSIGNED" and len(messages) > 1
        if has_reply:
            try:
                first = datetime.fromisoformat(str(messages[0].get("ts")).replace("Z", "+00:00"))
                reply = next((m for m in messages[1:] if str(m.get("role") or "").lower() in {"driver", "user", "inbound"}), messages[1])
                replied = datetime.fromisoformat(str(reply.get("ts")).replace("Z", "+00:00"))
                reply_times.append(max(0, (replied - first).total_seconds() / 60))
            except (ValueError, TypeError):
                pass
    replied_count = sum((str(item.get("callStatus") or item.get("status") or "").lower() in {"connected", "in-progress", "in_progress", "ended", "completed"} and len(item.get("messages") or []) > 1) if channel == "voice" else str(item.get("conversationStage") or "").upper() != "ASSIGNED" for item in active)
    return {"channel": channel, "messagesSentThisMonth": len(active), "responseRate": replied_count / len(active) * 100 if active else 0, "avgReplyTimeMinutes": sum(reply_times) / len(reply_times) if reply_times else None, "needsAttentionCount": attention, "confirmed": confirmed, "unconfirmed": len(active) - confirmed}


@router.get("/comms-overview")
def communications_overview():
    now_local = datetime.now(ZoneInfo("Asia/Manila"))
    month_start = now_local.date().replace(day=1)
    by_channel = []
    feed_conversations = {}
    for channel in ("email", "whatsapp", "sms", "voice"):
        feed = _n8n_conversations(channel, tolerate_errors=True)
        feed_conversations[channel] = feed.get("conversations") or []
        metrics = _overview_metrics(channel, feed_conversations[channel], month_start)
        if feed.get("error"):
            metrics["error"] = feed["error"]
        by_channel.append(metrics)
    total = sum(item["messagesSentThisMonth"] for item in by_channel)
    replies = sum(round(item["responseRate"] * item["messagesSentThisMonth"] / 100) for item in by_channel)
    reply_samples = [item["avgReplyTimeMinutes"] for item in by_channel if item["avgReplyTimeMinutes"] is not None]
    recent = []
    attention = []
    for channel, conversations in feed_conversations.items():
        for conversation in conversations:
            stamp = conversation.get("lastUpdated") or conversation.get("lastMessageAt")
            try:
                local_day = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Manila")).date() if stamp else None
            except ValueError:
                local_day = None
            messages = conversation.get("messages") or []
            if messages:
                message = max(messages, key=lambda item: str(item.get("ts") or ""))
                recent.append({"channel": channel, "driverName": conversation.get("driverName"), "truckPlate": conversation.get("truckPlate"), "lastMessageRole": "driver" if str(message.get("role") or "").lower() in {"driver", "user", "inbound"} else "agent", "lastMessagePreview": str(message.get("content") or "")[:80], "ts": message.get("ts"), "conversationStage": conversation.get("conversationStage")})
            if local_day is not None and local_day.replace(day=1) == month_start and (conversation.get("humanEscalated") or str(conversation.get("conversationStage") or "").upper() == "ISSUE_REPORTED"):
                driver_messages = [m for m in messages if str(m.get("role") or "").lower() in {"driver", "user", "inbound"}]
                latest = max(driver_messages or messages, key=lambda item: str(item.get("ts") or ""), default={})
                attention.append({"channel": channel, "driverName": conversation.get("driverName"), "truckPlate": conversation.get("truckPlate"), "soNumbers": conversation.get("soNumbers") or [], "conversationStage": conversation.get("conversationStage"), "lastUpdated": conversation.get("lastUpdated") or conversation.get("lastMessageAt"), "reasonPreview": str(latest.get("content") or "")[:120]})
    recent.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
    channel_map = {item["channel"]: item for item in by_channel}
    return {"messagesSentThisMonth": {"combined": total, **{channel: channel_map[channel]["messagesSentThisMonth"] for channel in channel_map}}, "responseRate": {"combined": replies / total * 100 if total else 0, **{channel: channel_map[channel]["responseRate"] for channel in channel_map}}, "avgReplyTimeMinutes": {"combined": sum(reply_samples) / len(reply_samples) if reply_samples else None, **{channel: channel_map[channel]["avgReplyTimeMinutes"] for channel in channel_map}}, "needsAttentionCount": sum(item["needsAttentionCount"] for item in by_channel), "confirmedVsUnconfirmed": {"confirmed": sum(item["confirmed"] for item in by_channel), "unconfirmed": sum(item["unconfirmed"] for item in by_channel)}, "period": {"type": "month", "start": month_start.isoformat()}, "channels": by_channel, "recentActivity": recent[:20], "attentionDetail": attention, "updatedAt": datetime.now(timezone.utc).isoformat()}

# n8n workflows call into this one - no user JWT exists on that side, same posture as
# comms.py's existing POST /webhooks/{provider} (unauthenticated by design, not an oversight).
public_router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])

# Populated once the matching n8n workflow (see COMMS_GATEWAY_SPEC.md section 3) exists and its
# webhook URL is known. Until then, sends are logged to message_log as "queued" and never
# actually reach a channel - this endpoint is usable end-to-end (data model, CRUD, timeline)
# without n8n being wired up yet.
_WEBHOOK_ENV_BY_AUDIENCE = {
    "driver": "DISPATCH_N8N_DRIVER_BROADCAST_URL",
    "customer": "DISPATCH_N8N_CUSTOMER_NOTIFY_URL",
    "internal": "DISPATCH_N8N_INTERNAL_ESCALATION_URL",
}

DRIVER_CHANNELS = ["email", "sms", "whatsapp"]
CUSTOMER_CHANNELS = ["email"]
INTERNAL_CHANNELS = ["email", "whatsapp", "sms"]
VALID_AUDIENCES = {"driver", "customer", "internal"}
VALID_CHANNELS = {"email", "sms", "whatsapp", "voice"}


# ---------------------------------------------------------------------------
# staff_directory CRUD
# ---------------------------------------------------------------------------


# n8n's "Logistics Staff Directory" DataTable is the only source of truth (2026-09-24) -
# staff_directory_cache refreshes from it every 30 min + on demand, never per request.
# Editing staff is done in n8n directly; there is no write path back into Neon any more.
# The one create path is POST /api/load-planning/assignments/new-driver (n8n webhook).


@router.get("/staff")
def list_staff(warehouse: str | None = None, active_only: bool = True):
    staff = staff_directory_cache.all_staff()
    if warehouse:
        staff = [s for s in staff if s.get("warehouse") == warehouse]
    if active_only:
        staff = [s for s in staff if s.get("active")]
    return sorted(staff, key=lambda s: s.get("name") or "")


@router.post("/staff/refresh")
def refresh_staff_directory():
    staff_directory_cache.refresh()
    return {"count": len(staff_directory_cache.all_staff()), "last_refreshed": staff_directory_cache.last_refreshed()}


@router.post("/staff", status_code=501)
def create_staff():
    raise HTTPException(501, "Staff directory is managed in n8n (Logistics Staff Directory DataTable), not here.")


@router.patch("/staff/{staff_id}", status_code=501)
def update_staff(staff_id: int):
    raise HTTPException(501, "Staff directory is managed in n8n (Logistics Staff Directory DataTable), not here.")


@router.delete("/staff/{staff_id}", status_code=501)
def delete_staff(staff_id: int):
    raise HTTPException(501, "Staff directory is managed in n8n (Logistics Staff Directory DataTable), not here.")


# ---------------------------------------------------------------------------
# customer_contacts CRUD
# ---------------------------------------------------------------------------


class CustomerContactBody(BaseModel):
    zoho_customer_id: str | None = None
    customer_name: str | None = None
    email: str | None = None
    phone: str | None = None
    whatsapp_number: str | None = None


@router.get("/customers")
def list_customer_contacts(search: str | None = None):
    rows = memory_tables.customer_contacts.list()
    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in " ".join(str(r.get(x) or "") for x in ("customer_name", "email", "zoho_customer_id")).lower()]
    return sorted(rows, key=lambda r: r.get("customer_name") or "")


@router.post("/customers", status_code=201)
def create_customer_contact(body: CustomerContactBody):
    if not body.email and not body.phone and not body.whatsapp_number:
        raise HTTPException(400, "At least one of email, phone, or whatsapp_number is required")
    return memory_tables.customer_contacts.create(**body.model_dump())


@router.patch("/customers/{contact_id}")
def update_customer_contact(contact_id: int, body: CustomerContactBody):
    row = memory_tables.customer_contacts.update(contact_id, **body.model_dump())
    if row is None:
        raise HTTPException(404, "Customer contact not found")
    return row


@router.delete("/customers/{contact_id}", status_code=204)
def delete_customer_contact(contact_id: int):
    if not memory_tables.customer_contacts.delete(contact_id):
        raise HTTPException(404, "Customer contact not found")


# ---------------------------------------------------------------------------
# message_templates CRUD
# ---------------------------------------------------------------------------


class TemplateBody(BaseModel):
    name: str
    audience: str
    channel: str
    subject: str | None = None
    body: str
    active: bool = True


def _validate_template(body: TemplateBody) -> None:
    if body.audience not in VALID_AUDIENCES:
        raise HTTPException(400, f"audience must be one of {', '.join(sorted(VALID_AUDIENCES))}")
    if body.channel not in VALID_CHANNELS:
        raise HTTPException(400, f"channel must be one of {', '.join(sorted(VALID_CHANNELS))}")


@router.get("/templates")
def list_templates(audience: str | None = None, channel: str | None = None):
    rows = memory_tables.message_templates.list(audience=audience, channel=channel)
    return sorted(rows, key=lambda r: r.get("name") or "")


@router.post("/templates", status_code=201)
def create_template(body: TemplateBody):
    _validate_template(body)
    return memory_tables.message_templates.create(**body.model_dump())


@router.patch("/templates/{template_id}")
def update_template(template_id: int, body: TemplateBody):
    _validate_template(body)
    row = memory_tables.message_templates.update(template_id, **body.model_dump())
    if row is None:
        raise HTTPException(404, "Template not found")
    return row


@router.delete("/templates/{template_id}", status_code=204)
def delete_template(template_id: int):
    if not memory_tables.message_templates.delete(template_id):
        raise HTTPException(404, "Template not found")


# ---------------------------------------------------------------------------
# message_log - Communications Gateway timeline
# ---------------------------------------------------------------------------


@router.get("/conversations")
def list_conversations(audience: str | None = None):
    """Groups message_log by recipient, newest activity first - the shape the Communications
    Gateway timeline renders, matching the [LEAD GEN] Get WhatsApp/SMS Conversations pattern
    referenced in the spec. 2026-09-24 (Step 6): message_log dropped from Neon - in-memory
    only now (process-local; history does not survive a restart)."""
    rows = sorted(memory_tables.message_log.list(audience=audience), key=lambda r: r["created_at"], reverse=True)

    grouped: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        key = row.get("recipient_contact") or row.get("recipient_name") or f"log-{row['id']}"
        if key not in grouped:
            grouped[key] = {
                "recipient_name": row.get("recipient_name"),
                "recipient_contact": row.get("recipient_contact"),
                "audience": row.get("audience"),
                "channels": [],
                "message_count": 0,
                "last_status": row.get("status"),
                "last_message_at": row["created_at"],
                "last_trigger_event": row.get("trigger_event"),
            }
            order.append(key)
        entry = grouped[key]
        entry["message_count"] += 1
        if row.get("channel") not in entry["channels"]:
            entry["channels"].append(row.get("channel"))

    return [grouped[key] for key in order]


@router.get("/messages")
def list_messages(recipient_contact: str | None = None, related_so_number: str | None = None):
    rows = memory_tables.message_log.list(recipient_contact=recipient_contact, related_so_number=related_so_number)
    return sorted(rows, key=lambda r: r["created_at"], reverse=True)


@router.get("/messages/{message_id}")
def get_message_detail(message_id: int):
    row = memory_tables.message_log.get(message_id)
    if row is None:
        raise HTTPException(404, "Message not found")
    return row


# ---------------------------------------------------------------------------
# Send orchestrator
# ---------------------------------------------------------------------------


def normalize_ph_phone(raw: str | None) -> str | None:
    """09XXXXXXXXX -> +639XXXXXXXXX (E.164). Leaves already-normalized or non-PH-shaped values
    alone rather than guessing. Mirrors what [DISPATCH] Normalize PH Phone Number does in n8n -
    kept here too so this backend's own send path doesn't depend on that subworkflow existing."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("09") and len(digits) == 11:
        return f"+63{digits[1:]}"
    if digits.startswith("639") and len(digits) == 12:
        return f"+{digits}"
    if raw.startswith("+"):
        return raw
    return raw


def _render(template: str, variables: dict) -> str:
    class _SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    return template.format_map(_SafeDict(**variables))


class SendMessageBody(BaseModel):
    audience: str
    recipient_id: int | None = None
    recipient_name: str | None = None
    recipient_email: str | None = None
    recipient_phone: str | None = None
    channels: list[str] | None = None
    template_name: str | None = None
    subject: str | None = None
    body: str | None = None
    variables: dict = {}
    trigger_event: str = "manual"
    related_so_number: str | None = None
    severity: str | None = None


def _resolve_recipient(body: SendMessageBody) -> tuple[str | None, str, str | None]:
    """Returns (name, primary_contact_for_log, resolved from staff/customer record if given)."""
    if body.audience in {"driver", "internal"} and body.recipient_id:
        staff = staff_directory_cache.get_by_id(body.recipient_id)
        if staff is None:
            raise HTTPException(404, "Staff recipient not found")
        return staff["name"], staff.get("email") or normalize_ph_phone(staff.get("phone")), staff.get("phone")
    if body.audience == "customer" and body.recipient_id:
        contact = memory_tables.customer_contacts.get(body.recipient_id)
        if contact is None:
            raise HTTPException(404, "Customer contact not found")
        return contact.get("customer_name"), contact.get("email"), contact.get("phone")
    if not body.recipient_name and not body.recipient_email and not body.recipient_phone:
        raise HTTPException(400, "Provide recipient_id, or recipient_name/email/phone directly")
    return body.recipient_name, body.recipient_email or normalize_ph_phone(body.recipient_phone), body.recipient_phone


async def _dispatch_webhook(audience: str, payload: dict) -> tuple[bool, str | None]:
    url = os.environ.get(_WEBHOOK_ENV_BY_AUDIENCE.get(audience, ""), "").strip()
    if not url:
        return False, None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
        if response.status_code >= 400:
            return False, f"n8n webhook returned HTTP {response.status_code}"
        try:
            data = response.json()
        except ValueError:
            data = {}
        return True, data.get("provider_message_id") if isinstance(data, dict) else None
    except httpx.HTTPError as exc:
        logger.warning("[Dispatch] n8n webhook call failed for audience=%s: %s", audience, exc)
        return False, None


@router.post("/send", status_code=201)
async def send_message(body: SendMessageBody):
    if body.audience not in VALID_AUDIENCES:
        raise HTTPException(400, f"audience must be one of {', '.join(sorted(VALID_AUDIENCES))}")

    recipient_name, primary_contact, raw_phone = _resolve_recipient(body)

    channels = body.channels
    if not channels:
        if body.audience == "driver":
            channels = DRIVER_CHANNELS
        elif body.audience == "customer":
            channels = CUSTOMER_CHANNELS
        else:
            channels = list(INTERNAL_CHANNELS) + (["voice"] if body.severity == "critical" else [])
    invalid = [c for c in channels if c not in VALID_CHANNELS]
    if invalid:
        raise HTTPException(400, f"Unknown channel(s): {', '.join(invalid)}")

    template_body = body.body
    template_subject = body.subject
    if body.template_name:
        template = next((t for t in memory_tables.message_templates.list(name=body.template_name, active=True)), None)
        if template is None:
            raise HTTPException(404, f"No active template named '{body.template_name}'")
        template_body = template_body or template.get("body")
        template_subject = template_subject or template.get("subject")

    logged: list[dict] = []
    for channel in channels:
        contact_for_channel = primary_contact
        if channel in {"sms", "whatsapp", "voice"}:
            contact_for_channel = normalize_ph_phone(raw_phone) or primary_contact

        rendered_body = _render(template_body, body.variables) if template_body else None

        row = memory_tables.message_log.create(
            audience=body.audience,
            recipient_name=recipient_name,
            recipient_contact=contact_for_channel,
            channel=channel,
            template_name=body.template_name,
            trigger_event=body.trigger_event,
            related_so_number=body.related_so_number,
            body=rendered_body,
            status="queued",
        )

        sent, provider_message_id = await _dispatch_webhook(
            body.audience,
            {
                "message_log_id": row["id"],
                "channel": channel,
                "recipient_name": recipient_name,
                "recipient_contact": contact_for_channel,
                "subject": template_subject,
                "body": rendered_body,
                "trigger_event": body.trigger_event,
                "related_so_number": body.related_so_number,
                "severity": body.severity,
            },
        )
        row = memory_tables.message_log.update(
            row["id"],
            status="sent" if sent else "queued",
            sent_at=datetime.now(timezone.utc) if sent else None,
            **({"provider_message_id": provider_message_id} if provider_message_id else {}),
        )
        logged.append(row)

    return {"messages": logged}


# ---------------------------------------------------------------------------
# Inbound status callback - n8n's [DISPATCH] Log Message / send workflows call this once a
# provider (Twilio/Gmail) confirms a real delivery/ack/failure, OR they can write to message_log
# directly via Postgres (same pattern the AR/Lead-gen n8n projects already use) - either path
# keeps this table as the single source of truth the timeline above reads from.
# ---------------------------------------------------------------------------


class MessageStatusBody(BaseModel):
    message_log_id: int | None = None
    provider_message_id: str | None = None
    status: str
    new_provider_message_id: str | None = None


def notify_packed_orders_batch(orders: list[dict]) -> None:
    """UNCALLED as of 2026-09-24 - reports.py stopped calling this (opening a report must not
    write to Neon/notify anyone as a side effect). Kept only as reference for whoever rebuilds
    the "order packed -> notify dispatcher" trigger as its own explicit path (likely in n8n).
    Each `order` dict needs so_number, customer, fulfillment_type, warehouse_tag.

    Interpretation note: Zoho package data carries no specific driver assignment, so "notify
    driver" is implemented as notifying the Dispatcher staff at the order's warehouse (METS/
    GLACIER) - the people who actually assign a driver - rather than guessing a driver identity
    that doesn't exist in the data. Flag to Rishi if a different mapping was intended.

    Never raises - a notification failure must not break the RGF Logistics Report response.
    Runs synchronously in the report request path but only does real work (memory write +
    webhook call) for orders not already logged, via one batched dedup check up front.
    """
    so_numbers = [o["so_number"] for o in orders if o.get("so_number")]
    if not so_numbers:
        return
    try:
        already_notified = {
            row["related_so_number"]
            for row in memory_tables.message_log.list(trigger_event="order_packed")
            if row.get("related_so_number") in so_numbers
        }
        new_orders = [o for o in orders if o.get("so_number") and o["so_number"] not in already_notified]
        if not new_orders:
            return

        staff_by_warehouse: dict[str, list[dict]] = {}
        webhook_url = os.environ.get(_WEBHOOK_ENV_BY_AUDIENCE["driver"], "").strip()

        for order in new_orders:
            warehouse = "GLACIER" if order.get("warehouse_tag") == "GLA" else order.get("warehouse_tag")
            if warehouse not in staff_by_warehouse:
                staff_by_warehouse[warehouse] = [
                    s for s in staff_directory_cache.all_staff()
                    if s.get("active") and s.get("warehouse") == warehouse and "dispatcher" in str(s.get("title") or "").lower()
                ]
            recipients = staff_by_warehouse[warehouse]
            if not recipients:
                continue

            so_number = order["so_number"]
            body_text = f"SO {so_number} ({order.get('customer') or 'customer'}) is packed and ready for dispatch at {order.get('warehouse_tag')}."
            for staff in recipients:
                for channel, contact in (
                    ("email", staff.get("email")),
                    ("sms", normalize_ph_phone(staff.get("phone"))),
                    ("whatsapp", normalize_ph_phone(staff.get("phone"))),
                ):
                    if not contact:
                        continue
                    row = memory_tables.message_log.create(
                        audience="driver",
                        recipient_name=staff.get("name"),
                        recipient_contact=contact,
                        channel=channel,
                        template_name="order_packed_auto",
                        trigger_event="order_packed",
                        related_so_number=so_number,
                        body=body_text,
                        status="queued",
                    )
                    if webhook_url:
                        try:
                            response = httpx.post(
                                webhook_url,
                                json={
                                    "message_log_id": row["id"],
                                    "channel": channel,
                                    "recipient_name": staff.get("name"),
                                    "recipient_contact": contact,
                                    "body": body_text,
                                    "trigger_event": "order_packed",
                                    "related_so_number": so_number,
                                },
                                timeout=8.0,
                            )
                            memory_tables.message_log.update(
                                row["id"],
                                status="sent" if response.status_code < 400 else "queued",
                                sent_at=datetime.now(timezone.utc) if response.status_code < 400 else None,
                            )
                        except httpx.HTTPError as exc:
                            logger.warning("[Dispatch] order_packed webhook call failed for %s: %s", so_number, exc)
    except Exception:
        logger.exception("[Dispatch] notify_packed_orders_batch failed")


@public_router.post("/webhooks/message-status")
def update_message_status(body: MessageStatusBody):
    row = None
    if body.message_log_id:
        row = memory_tables.message_log.get(body.message_log_id)
    elif body.provider_message_id:
        row = next(iter(memory_tables.message_log.list(provider_message_id=body.provider_message_id)), None)
    if row is None:
        raise HTTPException(404, "Message not found by message_log_id or provider_message_id")
    fields = {"status": body.status}
    if body.new_provider_message_id:
        fields["provider_message_id"] = body.new_provider_message_id
    return memory_tables.message_log.update(row["id"], **fields)
