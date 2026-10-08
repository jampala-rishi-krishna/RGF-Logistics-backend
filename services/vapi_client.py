"""Direct Vapi API client for the logistics voice flow (replaces the n8n voice workflows).

Payload builders mirror n8n "[LOGISTICS] Vapi Outbound Call" (cZDfRVfC48pViEny) and the
team-confirmation branch of "[LOGISTICS] Voice Call End Webhook" (QlvuUZ487Pd7iUIs) 1:1 -
same phone normalization, variableValues keys and firstMessage text.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

from services import voice_control

logger = logging.getLogger("vapi_client")

VAPI_BASE_URL = "https://api.vapi.ai"
DEFAULT_ASSISTANT_ID = "d495f7f1-eb0f-4906-81d3-caa09ede76ce"
DEFAULT_PHONE_NUMBER_ID = "f49a91dd-c34d-4f1a-b045-3aaabd4c9e3d"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)  # same as zoho_client
_MAX_ATTEMPTS = 3
_PH_E164 = re.compile(r"^\+63\d{10}$")


class VapiError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def voice_provider() -> str:
    return os.environ.get("VOICE_PROVIDER", "n8n").strip().lower() or "n8n"


def assistant_id() -> str:
    return os.environ.get("VAPI_ASSISTANT_ID", "").strip() or DEFAULT_ASSISTANT_ID


def phone_number_id() -> str:
    return os.environ.get("VAPI_PHONE_NUMBER_ID", "").strip() or DEFAULT_PHONE_NUMBER_ID


def _headers() -> dict:
    key = os.environ.get("VAPI_API_KEY", "").strip()
    if not key:
        raise VapiError("VAPI_API_KEY is not configured.")
    return {"Authorization": f"Bearer {key}"}


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """Retries only on 429/5xx (and transport errors), with a short backoff."""
    last_exc: Exception | None = None
    async with httpx.AsyncClient(base_url=VAPI_BASE_URL, timeout=_TIMEOUT) as client:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await client.request(method, path, headers=_headers(), **kwargs)
            except httpx.TransportError as exc:
                last_exc = exc
            else:
                if response.status_code != 429 and response.status_code < 500:
                    return response
                last_exc = VapiError(f"Vapi {method} {path} -> HTTP {response.status_code}: {response.text[:300]}", response.status_code)
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
    raise VapiError(str(last_exc)) from last_exc


def _json_or_raise(response: httpx.Response, what: str):
    if response.status_code >= 400:
        raise VapiError(f"{what} failed: HTTP {response.status_code}: {response.text[:300]}", response.status_code)
    return response.json()


def format_ph_phone(raw: str | None) -> str:
    """Identical to the n8n Code node: strip non-digits, drop a leading 0 or 63, prefix +63."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("0"):
        digits = digits[1:]
    elif digits.startswith("63"):
        digits = digits[2:]
    return f"+63{digits}" if digits else ""


def is_valid_ph_phone(formatted: str) -> bool:
    return bool(_PH_E164.match(formatted or ""))


def so_details_text(sales_orders: list[dict]) -> str:
    if not sales_orders:
        return "No sales order detail available."
    return ". ".join(
        f"{s.get('soNumber') or ''} for {s.get('clientName') or ''}, {s.get('totalKgs') or ''} kilograms, {s.get('totalPacks') or ''} packs"
        for s in sales_orders
    )


def build_driver_call(*, driver_name: str, driver_phone: str, truck_plate: str, warehouse: str, sales_orders: list[dict], metadata: dict) -> dict:
    formatted = format_ph_phone(driver_phone)
    if not is_valid_ph_phone(formatted):
        raise VapiError(f"Invalid driver phone number: {driver_phone}")
    driver_name = (driver_name or "").strip()
    first_name = driver_name.split(" ")[0] if driver_name else "Driver"
    return {
        "assistantId": assistant_id(),
        "phoneNumberId": phone_number_id(),
        "customer": {"number": formatted, "name": driver_name},
        "assistantOverrides": {
            "variableValues": {
                "driverName": driver_name,
                "driverFirstName": first_name,
                "truckPlate": truck_plate,
                "warehouse": warehouse,
                "soNumbers": ", ".join(str(s.get("soNumber") or "") for s in sales_orders),
                "soCount": str(len(sales_orders)),
                "soDetailsText": so_details_text(sales_orders),
                # Prompt treats unset as driver_assignment; set explicitly so the literal
                # "{{callPurpose}}" never reaches the model.
                "callPurpose": "driver_assignment",
            },
            "firstMessage": f"Hi, this is Martin calling from Rare Global Food Logistics. Am I speaking with {first_name}? I am calling about a new truck assignment for you.",
        },
        "metadata": {**metadata, "callPurpose": "driver_assignment"},
    }


def join_names(names: list[str]) -> str:
    names = [n for n in dict.fromkeys(names) if n]
    if len(names) > 1:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return names[0] if names else "the driver"


def build_team_confirmation_call(*, staff_name: str, staff_phone: str, driver_names: list[str], truck_plate: str, warehouse: str, sales_orders: list[dict], metadata: dict) -> dict:
    formatted = format_ph_phone(staff_phone)
    if not is_valid_ph_phone(formatted):
        raise VapiError(f"Invalid staff phone number: {staff_phone}")
    joined = join_names(driver_names)
    plural = len({n for n in driver_names if n}) > 1
    first_name = (staff_name or "").split(" ")[0] or "there"
    plate = truck_plate or "-"
    wh = warehouse or "-"
    return {
        "assistantId": assistant_id(),
        "phoneNumberId": phone_number_id(),
        "customer": {"number": formatted, "name": staff_name},
        "assistantOverrides": {
            "variableValues": {
                "driverName": joined,
                "truckPlate": plate,
                "warehouse": wh,
                "soNumbers": ", ".join(str(s.get("soNumber") or "") for s in sales_orders),
                "soDetailsText": so_details_text(sales_orders),
                "callPurpose": "team_confirmation",
            },
            "firstMessage": f"Hi {first_name}, this is Martin from RGF Logistics. Quick confirmation call for truck {plate} at {wh}: {joined}{' are' if plural else ' is'} the assigned driver{'s' if plural else ''} on this route. Do you have any questions about the assignment?",
        },
        "metadata": {**metadata, "callPurpose": "team_confirmation"},
    }


async def create_call(payload: dict) -> dict:
    # Last line of defence: no outbound call is ever created while AI voice calls are paused.
    if not voice_control.is_active():
        raise voice_control.VoiceCallsPaused("AI voice calls are paused")
    # /call/phone, the endpoint the n8n workflows used.
    return _json_or_raise(await _request("POST", "/call/phone", json=payload), "Vapi create call")


async def list_calls(*, limit: int = 100, created_at_gt: str | None = None, created_at_lt: str | None = None) -> list[dict]:
    params: dict = {"assistantId": assistant_id(), "limit": limit}
    if created_at_gt:
        params["createdAtGt"] = created_at_gt
    if created_at_lt:
        params["createdAtLt"] = created_at_lt
    body = _json_or_raise(await _request("GET", "/call", params=params), "Vapi list calls")
    return body if isinstance(body, list) else body.get("results") or []


async def get_call(call_id: str) -> dict:
    return _json_or_raise(await _request("GET", f"/call/{call_id}"), "Vapi get call")
