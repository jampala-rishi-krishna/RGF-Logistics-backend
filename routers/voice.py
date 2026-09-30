"""Logistics voice API, sourced directly from Vapi (no n8n, no transcript storage in Neon)."""
from __future__ import annotations

import hmac
import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from auth.dependencies import require_role
from services import staff_directory_cache, vapi_client, voice_calls

logger = logging.getLogger("voice")

router = APIRouter(prefix="/api/voice", tags=["voice"], dependencies=[Depends(require_role("admin", "dispatcher", "warehouse"))])
webhook_router = APIRouter(tags=["voice"])
admin_router = APIRouter(prefix="/api/admin", tags=["voice"], dependencies=[Depends(require_role("admin"))])


def _vapi_http_error(exc: vapi_client.VapiError) -> HTTPException:
    return HTTPException(404 if exc.status_code == 404 else 502, str(exc))


@router.get("/calls")
async def list_calls(limit: int = 50, created_after: str | None = None, created_before: str | None = None):
    try:
        calls = await voice_calls.list_calls(limit=max(1, min(limit, 100)), created_at_gt=created_after, created_at_lt=created_before)
    except vapi_client.VapiError as exc:
        raise _vapi_http_error(exc) from exc
    return {"count": len(calls), "calls": [voice_calls.to_call_summary(c) for c in calls]}


@router.get("/calls/{call_id}")
async def call_detail(call_id: str):
    try:
        return voice_calls.to_call_detail(await vapi_client.get_call(call_id))
    except vapi_client.VapiError as exc:
        raise _vapi_http_error(exc) from exc


async def recording_response(call_id: str) -> StreamingResponse:
    try:
        url = voice_calls.recording_url(await vapi_client.get_call(call_id))
    except vapi_client.VapiError as exc:
        raise _vapi_http_error(exc) from exc
    if not url:
        raise HTTPException(404, "This call has no recording.")
    client = httpx.AsyncClient(timeout=60, follow_redirects=True)
    # Presigned object-storage URL: no Vapi Authorization header (R2 rejects it).
    upstream = await client.send(client.build_request("GET", url), stream=True)
    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(upstream.status_code, "The voice recording is unavailable.")

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(body(), media_type=upstream.headers.get("content-type") or "audio/wav")


@router.get("/calls/{call_id}/recording")
async def call_recording(call_id: str):
    return await recording_response(call_id)


def _secret_ok(request: Request) -> bool:
    expected = os.environ.get("VAPI_SERVER_SECRET", "").strip()
    if not expected:
        return False
    header = request.headers.get("x-vapi-secret")
    auth = request.headers.get("authorization") or ""
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    return any(v is not None and hmac.compare_digest(v, expected) for v in (header, bearer))


@webhook_router.post("/vapi/webhook")
async def vapi_webhook(request: Request):
    if not _secret_ok(request):
        logger.warning("Rejected /vapi/webhook: bad or missing secret (headers: %s)", sorted(request.headers.keys()))
        raise HTTPException(401, "Unauthorized")
    body = await request.json()
    message = body.get("message") or {}
    kind = message.get("type")
    if kind == "status-update":
        await voice_calls.handle_status_update(message)
    elif kind == "end-of-call-report":
        await voice_calls.handle_end_of_call(message)
    elif kind == "tool-calls":
        return await voice_calls.handle_tool_calls(message)
    return {"ok": True}


@admin_router.post("/refresh-staff")
def refresh_staff():
    staff_directory_cache.refresh()
    return {"source": staff_directory_cache.source(), "staff": len(staff_directory_cache.all_staff()), "notify": len(staff_directory_cache.notify_list()), "last_refreshed": staff_directory_cache.last_refreshed()}
