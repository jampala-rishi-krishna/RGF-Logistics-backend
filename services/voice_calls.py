"""Logistics voice flow without n8n (VOICE_PROVIDER=direct).

- Call logs/transcripts/recordings are read from the Vapi API on demand - nothing about a
  call is stored in Neon.
- Live call state (status updates, tool results) is process memory only, pushed to the
  dashboard over the existing /ws/fleet WebSocket. It is rebuilt from Vapi's own call
  messages when missing (e.g. after a restart), so nothing depends on it surviving.
- Mirrors the n8n workflows it replaces: Vapi Outbound Call (cZDfRVfC48pViEny), Voice Tool -
  Confirm Assignment (2wfQCbNcmKTHUCwK), Voice Call End Webhook (QlvuUZ487Pd7iUIs), Voice
  Call Detail (c4dMyIWXk9oT5wQt), Recording Proxy (aJhY8ME6LB7QBiEZ) and Get Voice
  Conversations (vvPaaiZYig1OfaSi).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone


from services import staff_directory_cache, vapi_client
from services.ws_manager import manager

logger = logging.getLogger("voice_calls")

NO_ANSWER_REASONS = {"customer-busy", "customer-did-not-answer", "voicemail"}
CONFIRM_TOOL = "confirmAssignment"
REPORT_TOOL = "reportIssue"
CONFIRM_RESULT = "Got it, thanks for confirming. Drive safe."
LIST_CACHE_TTL_SECONDS = 60
# Vapi's end-of-call webhook can't reach this backend (no public URL), so the team
# confirmation trigger polls the batch's calls instead.
BATCH_POLL_SECONDS = 15
BATCH_WATCH_MAX_SECONDS = 45 * 60

_lock = threading.Lock()
# call_id -> {"callStatus", "assignmentConfirmed", "humanEscalated", "status", "updatedAt", ...}
_live: dict[str, dict] = {}
# assignment_id -> {"calls": {call_id: ended_bool}, "drivers": [...], "truckPlate", ..., "teamNotified"}
_batches: dict[str, dict] = {}
_list_cache: dict[tuple, tuple[float, list]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Vapi call -> dashboard shapes
# ---------------------------------------------------------------------------

def find_key_deep(obj, key: str, seen: set | None = None):
    """Same as the n8n findKeyDeep: first non-empty string value for `key` anywhere."""
    seen = seen if seen is not None else set()
    if not isinstance(obj, (dict, list)) or id(obj) in seen:
        return None
    seen.add(id(obj))
    if isinstance(obj, dict):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
        children = obj.values()
    else:
        children = obj
    for child in children:
        found = find_key_deep(child, key, seen)
        if found:
            return found
    return None


def recording_url(call: dict) -> str:
    return (
        find_key_deep(call, "presignedMonoUrl")
        or find_key_deep(call, "recordingUrl")
        or ((call.get("artifact") or {}).get("recording") or {}).get("mono", {}).get("combinedUrl")
        or ""
    )


def _messages(call: dict) -> list[dict]:
    return call.get("messages") or (call.get("artifact") or {}).get("messages") or []


def _tool_names_called(call: dict) -> set[str]:
    names = set()
    for message in _messages(call):
        for tool_call in message.get("toolCalls") or []:
            name = (tool_call.get("function") or {}).get("name")
            if name:
                names.add(name)
    return names


def _vars(call: dict) -> dict:
    return (call.get("assistantOverrides") or {}).get("variableValues") or {}


def call_state(call: dict) -> dict:
    """callStatus / assignmentConfirmed / humanEscalated with the n8n data table's semantics."""
    with _lock:
        live = dict(_live.get(call.get("id"), {}))
    tools = _tool_names_called(call)
    confirmed = bool(live.get("assignmentConfirmed")) or CONFIRM_TOOL in tools
    escalated = bool(live.get("humanEscalated")) or REPORT_TOOL in tools or call.get("endedReason") == "assistant-forwarded-call"
    status = call.get("status") or live.get("status")
    ended_reason = call.get("endedReason") or live.get("endedReason") or ""
    if status == "ended":
        call_status = "NO_ANSWER" if ended_reason in NO_ANSWER_REASONS else "ENDED"
    elif escalated:
        call_status = "ISSUE_REPORTED"
    elif confirmed:
        call_status = "CONFIRMED"
    else:
        call_status = "INITIATED"
    return {"callStatus": call_status, "assignmentConfirmed": confirmed, "humanEscalated": escalated, "status": status, "endedReason": ended_reason}


def call_summary(call: dict) -> str:
    return (call.get("analysis") or {}).get("summary") or call.get("summary") or ""


def to_conversation(call: dict) -> dict:
    """Row shape of the n8n 'Get Voice Conversations' feed the dashboard's Voice tab reads."""
    variables = _vars(call)
    state = call_state(call)
    transcript = ""
    if state["status"] == "ended":
        transcript = (call_summary(call) or (call.get("artifact") or {}).get("transcript") or call.get("transcript") or "(no transcript available)")
        if state["endedReason"]:
            transcript += f"\n[Call ended: {state['endedReason']}]"
    return {
        "channel": "voice",
        "contact": (call.get("customer") or {}).get("number") or "",
        "driverName": variables.get("driverName") or (call.get("customer") or {}).get("name") or "",
        "truckPlate": variables.get("truckPlate") or "",
        "warehouse": variables.get("warehouse") or "",
        "soNumbers": variables.get("soNumbers") or "",
        "callId": call.get("id"),
        "callTimestamp": call.get("startedAt") or call.get("createdAt"),
        "callStatus": state["callStatus"],
        "assignmentConfirmed": state["assignmentConfirmed"],
        "humanEscalated": state["humanEscalated"],
        "transcript": transcript,
        "lastUpdated": call.get("updatedAt") or call.get("createdAt"),
    }


def _duration_seconds(call: dict) -> int | None:
    started, ended = call.get("startedAt"), call.get("endedAt")
    if not (started and ended):
        return None
    try:
        return round((datetime.fromisoformat(ended.replace("Z", "+00:00")) - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds())
    except ValueError:
        return None


def to_call_summary(call: dict) -> dict:
    """Trimmed row for GET /api/voice/calls."""
    metadata = call.get("metadata") or {}
    variables = _vars(call)
    started, ended = call.get("startedAt"), call.get("endedAt")
    return {
        "id": call.get("id"),
        "customer": call.get("customer") or {},
        "vehicle": metadata.get("vehicle") or variables.get("truckPlate"),
        "driver_id": metadata.get("driver_id"),
        "driver_name": variables.get("driverName") or (call.get("customer") or {}).get("name"),
        "assignment_id": metadata.get("assignment_id"),
        "call_purpose": metadata.get("callPurpose") or variables.get("callPurpose") or "driver_assignment",
        "status": call.get("status"),
        "callStatus": call_state(call)["callStatus"],
        "endedReason": call.get("endedReason"),
        "startedAt": started or call.get("createdAt"),
        "endedAt": ended,
        "duration": _duration_seconds(call),
        "cost": call.get("cost") or 0,
        "summary": call_summary(call),
    }


def to_call_detail(call: dict) -> dict:
    """Same shape and transcript->turns parsing as the n8n 'Voice Call Detail' workflow."""
    variables = _vars(call)
    turns = []
    for message in _messages(call):
        role = message.get("role")
        seconds = message.get("secondsFromStart") or 0
        if role == "bot":
            turns.append({"role": "assistant", "text": message.get("message") or "", "secondsFromStart": seconds})
        elif role == "user":
            turns.append({"role": "driver", "text": message.get("message") or "", "secondsFromStart": seconds})
        elif role == "tool_calls" and message.get("toolCalls"):
            name = (message["toolCalls"][0].get("function") or {}).get("name") or "unknown"
            turns.append({"role": "system", "text": f"Tool called: {name}", "secondsFromStart": seconds})
        elif role == "tool_call_result":
            text = f"Result: {message['name']} → {message.get('result') or ''}" if message.get("name") else (message.get("result") or "")
            turns.append({"role": "system", "text": text, "secondsFromStart": seconds})
    return {
        "callId": call.get("id"),
        "driverName": variables.get("driverName") or (call.get("customer") or {}).get("name") or "Unknown",
        "phone": (call.get("customer") or {}).get("number") or "",
        "truckPlate": variables.get("truckPlate") or "",
        "warehouse": variables.get("warehouse") or "",
        "soNumbers": variables.get("soNumbers") or "",
        "status": call.get("status"),
        "endedReason": call.get("endedReason") or "",
        "startedAt": call.get("createdAt"),
        "duration": _duration_seconds(call),
        "cost": call.get("cost") or 0,
        "audioUrl": recording_url(call),
        "callSummary": call_summary(call),
        "transcript": (call.get("artifact") or {}).get("transcript") or call.get("transcript") or "",
        "turns": turns,
    }


async def list_calls(*, limit: int = 100, created_at_gt: str | None = None, created_at_lt: str | None = None) -> list[dict]:
    key = (limit, created_at_gt, created_at_lt)
    cached = _list_cache.get(key)
    if cached and time.monotonic() - cached[0] < LIST_CACHE_TTL_SECONDS:
        return cached[1]
    calls = await vapi_client.list_calls(limit=limit, created_at_gt=created_at_gt, created_at_lt=created_at_lt)
    _list_cache[key] = (time.monotonic(), calls)
    return calls


def invalidate_list_cache() -> None:
    _list_cache.clear()


async def conversations_feed() -> dict:
    """Drop-in for the n8n voice feed: one row per driver phone (latest call), driver calls only."""
    latest: dict[str, dict] = {}
    for call in await list_calls(limit=100):
        purpose = (call.get("metadata") or {}).get("callPurpose") or _vars(call).get("callPurpose")
        if purpose == "team_confirmation":
            continue
        phone = (call.get("customer") or {}).get("number") or call.get("id")
        if phone not in latest or (call.get("createdAt") or "") > (latest[phone].get("createdAt") or ""):
            latest[phone] = call
    conversations = [to_conversation(c) for c in latest.values()]
    conversations.sort(key=lambda c: c.get("lastUpdated") or "", reverse=True)
    return {"count": len(conversations), "conversations": conversations}


# ---------------------------------------------------------------------------
# Outbound calls (replaces the n8n voice-call webhook in the assignment fan-out)
# ---------------------------------------------------------------------------

async def place_assignment_calls(*, assignment_id: str, salesorder_ids: list[str], vehicle_id: str, truck_plate: str, warehouse: str, sales_orders: list[dict], drivers: list[dict]) -> list[str]:
    """One call per selected driver, same payload as n8n."""
    call_ids: list[str] = []
    batch = {"calls": {}, "drivers": [d.get("name") for d in drivers], "truckPlate": truck_plate, "warehouse": warehouse, "vehicle": vehicle_id, "salesOrders": sales_orders, "teamNotified": False}
    for driver in drivers:
        try:
            payload = vapi_client.build_driver_call(
                driver_name=driver.get("name") or "",
                driver_phone=driver.get("phone") or "",
                truck_plate=truck_plate,
                warehouse=warehouse,
                sales_orders=sales_orders,
                metadata={"assignment_id": assignment_id, "driver_id": driver.get("id"), "vehicle": vehicle_id},
            )
            call = await vapi_client.create_call(payload)
        except vapi_client.VapiError as exc:
            logger.error("Vapi call failed driver=%s assignment=%s: %s", driver.get("name"), assignment_id, exc)
            continue
        call_ids.append(call["id"])
        batch["calls"][call["id"]] = False
        with _lock:
            _live[call["id"]] = {"status": call.get("status") or "queued", "updatedAt": _now_iso(), "assignment_id": assignment_id}
        logger.info("Vapi call placed id=%s driver=%s assignment=%s", call["id"], driver.get("name"), assignment_id)
    if call_ids:
        with _lock:
            _batches[assignment_id] = batch
        invalidate_list_cache()
    return call_ids


def place_assignment_calls_sync(**kwargs) -> list[str]:
    """Entry point for the assignment fan-out thread pool."""
    call_ids = asyncio.run(place_assignment_calls(**kwargs))
    if call_ids:
        # Own thread: watching can take the length of a call, and the fan-out pool is small.
        threading.Thread(target=asyncio.run, args=(_watch_batch(kwargs["assignment_id"]),), name="voice-batch-watch", daemon=True).start()
    return call_ids


async def _watch_batch(assignment_id: str) -> None:
    """Poll Vapi until every driver call in the batch has ended, then place the team
    confirmation calls. Same effect as the end-of-call webhook path; _maybe_call_team's
    teamNotified flag keeps the two from both firing."""
    deadline = time.monotonic() + BATCH_WATCH_MAX_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(BATCH_POLL_SECONDS)
        with _lock:
            batch = _batches.get(assignment_id)
            if not batch or batch["teamNotified"]:
                return
            pending = [call_id for call_id, ended in batch["calls"].items() if not ended]
        for call_id in pending:
            try:
                call = await vapi_client.get_call(call_id)
            except vapi_client.VapiError as exc:
                logger.warning("Batch watch could not read call=%s assignment=%s: %s", call_id, assignment_id, exc)
                continue
            if call.get("status") == "ended":
                with _lock:
                    batch["calls"][call_id] = True
                    _live.setdefault(call_id, {}).update(status="ended", endedReason=call.get("endedReason") or "", updatedAt=_now_iso())
                # No _push here: the WebSockets belong to the main event loop, not this
                # thread's; the Voice tab picks the change up on its next poll.
                invalidate_list_cache()
        await _maybe_call_team(assignment_id)
    logger.warning("Batch watch gave up after %ss assignment=%s", BATCH_WATCH_MAX_SECONDS, assignment_id)


async def _maybe_call_team(assignment_id: str) -> None:
    """Mirror of the n8n Batch Ready Gate: once every driver call in the assignment has
    ended, place one team_confirmation call to each person on the Notify List."""
    with _lock:
        batch = _batches.get(assignment_id)
        if not batch or batch["teamNotified"] or not all(batch["calls"].values()):
            return
        batch["teamNotified"] = True
    for contact in staff_directory_cache.notify_list():
        try:
            payload = vapi_client.build_team_confirmation_call(
                staff_name=contact.get("name") or "",
                staff_phone=contact.get("phone") or "",
                driver_names=batch["drivers"],
                truck_plate=batch["truckPlate"],
                warehouse=batch["warehouse"],
                sales_orders=batch["salesOrders"],
                metadata={"assignment_id": assignment_id, "vehicle": batch["vehicle"]},
            )
            call = await vapi_client.create_call(payload)
            logger.info("Team confirmation call placed id=%s to=%s assignment=%s", call.get("id"), contact.get("name"), assignment_id)
        except vapi_client.VapiError as exc:
            logger.error("Team confirmation call failed to=%s assignment=%s: %s", contact.get("name"), assignment_id, exc)
    invalidate_list_cache()


# ---------------------------------------------------------------------------
# /vapi/webhook server messages
# ---------------------------------------------------------------------------

async def _push(call_id: str, event: str, extra: dict | None = None) -> None:
    with _lock:
        state = dict(_live.get(call_id, {}))
    await manager.broadcast({"type": "voice_call", "event": event, "callId": call_id, **state, **(extra or {})})


async def handle_status_update(message: dict) -> None:
    call = message.get("call") or {}
    call_id = call.get("id")
    if not call_id:
        return
    with _lock:
        state = _live.setdefault(call_id, {})
        state.update(status=message.get("status"), updatedAt=_now_iso())
        if message.get("endedReason"):
            state["endedReason"] = message["endedReason"]
    invalidate_list_cache()
    await _push(call_id, "status-update")


async def handle_end_of_call(message: dict) -> None:
    call = message.get("call") or {}
    call_id = call.get("id")
    if not call_id:
        return
    summary = message.get("summary") or (message.get("analysis") or {}).get("summary") or ""
    with _lock:
        state = _live.setdefault(call_id, {})
        state.update(status="ended", endedReason=message.get("endedReason") or "", updatedAt=_now_iso())
        assignment_id = state.get("assignment_id") or (call.get("metadata") or {}).get("assignment_id")
        purpose = (call.get("metadata") or {}).get("callPurpose")
        if assignment_id and purpose != "team_confirmation" and assignment_id in _batches:
            _batches[assignment_id]["calls"][call_id] = True
    invalidate_list_cache()
    await _push(call_id, "ended", {"summary": summary})
    if assignment_id and purpose != "team_confirmation":
        await _maybe_call_team(assignment_id)


async def handle_tool_calls(message: dict) -> dict:
    call = message.get("call") or {}
    call_id = call.get("id")
    results = []
    for tool_call in message.get("toolCallList") or message.get("toolCalls") or []:
        name = (tool_call.get("function") or {}).get("name") or tool_call.get("name")
        if name == CONFIRM_TOOL:
            if call_id:
                with _lock:
                    _live.setdefault(call_id, {}).update(assignmentConfirmed=True, callStatus="CONFIRMED", updatedAt=_now_iso())
                await _push(call_id, "confirmed")
            result = CONFIRM_RESULT
        else:
            # reportIssue keeps its own n8n server URL on the tool, so it never lands here.
            logger.warning("Unhandled Vapi tool call name=%s call=%s", name, call_id)
            result = "Sorry, I couldn't do that right now."
        results.append({"toolCallId": tool_call.get("id"), "result": result})
    invalidate_list_cache()
    return {"results": results}
