from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from auth.dependencies import CurrentUser, bearer_scheme, get_current_user
from services import memory_tables
from services.agent_tools import ACTION_TOOLS, READ_TOOLS, InternalRequestError
from services.audit import write_audit_log
from services.openai_client import create_response, extract_text

router = APIRouter(prefix="/agent", tags=["agent"])

MAX_TOOL_LOOP_ITERATIONS = 5

# 2026-09-24 (Step 6): conversations/messages (AI chat history) dropped from Neon - in-memory
# only now (process-local; chat history does not survive a restart).


def _get_or_create_conversation(conversation_id: int | None, user_id: int) -> int:
    if conversation_id:
        conv = memory_tables.conversations.get(conversation_id)
        if conv:
            return conv["id"]
    conv = memory_tables.conversations.create(subject_type="ai_agent", subject_id=str(user_id), channel="ai_agent", status="active")
    return conv["id"]


def _insert_message(conversation_id: int, direction: str, content: str) -> None:
    memory_tables.messages.create(
        conversation_id=str(conversation_id),
        direction=direction,
        channel="ai_agent",
        content_ref=content,
        status="delivered" if direction == "outbound" else "received",
        sent_at=datetime.now(timezone.utc),
    )


def _load_conversation_input(conversation_id: int) -> list[dict]:
    messages = sorted(memory_tables.messages.list(conversation_id=str(conversation_id)), key=lambda m: m["created_at"])
    return [{"role": "user" if m["direction"] == "inbound" else "assistant", "content": m["content_ref"]} for m in messages]


class ChatBody(BaseModel):
    message: str
    conversationId: int | None = None


@router.post("/chat")
async def chat(
    body: ChatBody,
    current_user: CurrentUser = Depends(get_current_user),
):
    message = body.message.strip() if body.message else ""
    if not message:
        raise HTTPException(400, "message is required")

    conversation_id = _get_or_create_conversation(body.conversationId, current_user.id)
    _insert_message(conversation_id, "inbound", message)

    current_input = _load_conversation_input(conversation_id)

    for _ in range(MAX_TOOL_LOOP_ITERATIONS):
        response = await create_response(current_input)
        function_calls = [item for item in (response.output or []) if getattr(item, "type", None) == "function_call"]

        if not function_calls:
            reply_text = extract_text(response) or "(no response)"
            _insert_message(conversation_id, "outbound", reply_text)
            return {"reply": reply_text, "conversationId": conversation_id}

        output_items = []
        for call in function_calls:
            try:
                args = json.loads(call.arguments) if call.arguments else {}
            except json.JSONDecodeError:
                args = {}

            if call.name in ACTION_TOOLS:
                # Human-approval gate: an ACTION tool's call is never auto-executed here. Stop
                # the loop entirely and hand it back for explicit confirmation.
                return {
                    "reply": None,
                    "conversationId": conversation_id,
                    "pendingAction": {"tool": call.name, "args": args, "callId": call.call_id},
                }

            tool_fn = READ_TOOLS.get(call.name)
            try:
                result = await tool_fn(args) if tool_fn else {"error": f"Unknown tool: {call.name}"}
            except Exception as e:
                result = {"error": str(e) or "Tool call failed"}
            output_items.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result)})

        current_input = current_input + [c.model_dump() for c in function_calls] + output_items

    fallback = "I wasn't able to finish that within the allowed number of steps - please try a more specific question."
    _insert_message(conversation_id, "outbound", fallback)
    return {"reply": fallback, "conversationId": conversation_id}


class ConfirmActionBody(BaseModel):
    conversationId: int
    tool: str
    args: dict = {}


@router.post("/confirm-action")
async def confirm_action(
    body: ConfirmActionBody,
    current_user: CurrentUser = Depends(get_current_user),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    tool_fn = ACTION_TOOLS.get(body.tool)
    if tool_fn is None:
        raise HTTPException(400, f"Unknown or non-action tool: {body.tool}")

    # Forwards the caller's own JWT, so the target endpoint's own role gate (e.g. alerts'
    # dispatcher/admin check) applies exactly as it would to any other caller - the agent never
    # bypasses it.
    try:
        result = await tool_fn(body.args or {}, credentials.credentials)
    except InternalRequestError as e:
        raise HTTPException(e.status_code, str(e))

    write_audit_log(
        actor_id=str(current_user.id),
        action=f"agent_confirmed_{body.tool}",
        target_entity=body.tool,
        target_id=json.dumps(body.args or {}),
        details={"args": body.args, "result": result},
    )

    reply_text = f"Done - {body.tool} completed."
    _insert_message(body.conversationId, "outbound", reply_text)
    return {"reply": reply_text, "conversationId": body.conversationId}
