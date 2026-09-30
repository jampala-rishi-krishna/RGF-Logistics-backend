from __future__ import annotations

import os
import re

import httpx

from services import optimizer
from services.google_maps import OrsError, fetch_route_matrix

# Every tool is a thin HTTP wrapper around this same app's own routers (loopback over HTTP),
# not a re-implementation of their query/validation/role-gating logic - ported verbatim from
# ai-agent-module/lib/tools.js's design rationale: this is the only clean way for a tool to
# inherit whatever auth gate the target endpoint already enforces (e.g. alerts' dispatcher/admin
# check on acknowledge) without duplicating it here.

INTERNAL_API_BASE_URL = os.environ.get("INTERNAL_API_BASE_URL", "http://127.0.0.1:8003")

ROWID_PATTERN = re.compile(r"^\d{1,20}$")


class InternalRequestError(Exception):
    def __init__(self, message: str, status_code: int, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


async def internal_request(method: str, path: str, *, query: dict | None = None, body=None, token: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(base_url=INTERNAL_API_BASE_URL, timeout=20.0) as client:
        res = await client.request(method, path, params=query, json=body, headers=headers)
    data = None
    if res.text:
        try:
            data = res.json()
        except ValueError:
            data = res.text
    if res.status_code >= 400:
        message = (data or {}).get("detail") if isinstance(data, dict) else None
        raise InternalRequestError(message or f"{method} {path} returned {res.status_code}", res.status_code, data)
    return data


# ---- Read tools ----------------------------------------------------------------------------


async def get_vehicle_status(args: dict) -> dict:
    plate_or_id = args["plateOrId"]
    if ROWID_PATTERN.match(str(plate_or_id)):
        try:
            return await internal_request("GET", f"/vehicles/{plate_or_id}")
        except InternalRequestError as e:
            if e.status_code == 404:
                return {"error": f"No vehicle found with id {plate_or_id}"}
            raise
    all_vehicles = await internal_request("GET", "/vehicles")
    normalized = re.sub(r"[^A-Z0-9]", "", str(plate_or_id).upper())
    for v in all_vehicles:
        if re.sub(r"[^A-Z0-9]", "", str(v.get("plate_no") or "").upper()) == normalized:
            return v
    return {"error": f"No vehicle found with plate {plate_or_id}"}


async def get_active_alerts(args: dict) -> dict:
    query = {"status": "open"}
    if args.get("severity"):
        query["severity"] = args["severity"]
    return await internal_request("GET", "/alerts", query=query)


async def get_order_status(args: dict) -> dict:
    order_id = args["orderId"]
    try:
        return await internal_request("GET", f"/orders/{order_id}")
    except InternalRequestError as e:
        if e.status_code == 404:
            return {"error": f"No order found with id {order_id}"}
        raise


async def suggest_reroute(args: dict) -> dict:
    """Read-only preview - never calls the persisting POST /routes/optimize, and never applies
    anything (that's why it's a READ tool, not an ACTION tool)."""
    vehicle_id = args["vehicleId"]
    try:
        vehicle = await internal_request("GET", f"/vehicles/{vehicle_id}")
    except InternalRequestError as e:
        if e.status_code == 404:
            return {"error": f"No vehicle found with id {vehicle_id}"}
        raise

    manifests = await internal_request("GET", "/manifests")
    manifest = next((m for m in manifests if str(m.get("vehicle_id")) == str(vehicle_id)), None)
    if manifest is None:
        return {"message": f"Vehicle {vehicle_id} has no active load manifest / assigned route to suggest a reroute for."}

    stops = await internal_request("GET", f"/routes/{manifest['route_id']}/stops")
    if not stops:
        return {"message": f"Route {manifest['route_id']} has no stops recorded yet."}
    if vehicle.get("current_lat") is None or vehicle.get("current_lng") is None:
        return {"message": f"Vehicle {vehicle_id} has no current Cartrack GPS position - cannot compute a reroute preview."}
    missing = [s for s in stops if s.get("lat") is None or s.get("lng") is None]
    if missing:
        return {"message": f"{len(missing)} of this route's stops are missing coordinates."}

    # vehicle_operating_profiles isn't exposed as a tool, and this tool must never fabricate a
    # capacity number - deliberately non-binding, high enough to never be the limiting factor
    # for this quick chat-assistant sequencing preview (matches the original's rationale
    # exactly: never persists anything, so a non-binding capacity here is honest, not misleading).
    capacity_kg = 1_000_000.0

    try:
        locations = [[vehicle["current_lng"], vehicle["current_lat"]]] + [[s["lng"], s["lat"]] for s in stops]
        matrix = await fetch_route_matrix(locations)
    except OrsError as e:
        return {"message": f"Road travel data unavailable - could not compute a reroute suggestion right now ({e})."}

    opt_request = optimizer.OptimizeRequest(
        vehicles=[
            optimizer.Vehicle(
                id=str(vehicle_id),
                capacity_kg=capacity_kg,
                start_node=0,
                end_node=len(stops),
                shift_start=0,
                shift_end=1440,
                temperature_capabilities=["ambient"],
            )
        ],
        shipments=[
            optimizer.Shipment(
                id=str(s["id"]),
                node=i + 1,
                demand_kg=0,
                service_time_min=0,
                time_window_start=0,
                time_window_end=1440,
                temperature_requirement="ambient",
                priority=1,
            )
            for i, s in enumerate(stops)
        ],
        distance_matrix_km=matrix["distance_matrix_km"],
        duration_matrix_min=matrix["duration_matrix_min"],
        objective="fastest",
    )
    result = optimizer.solve(opt_request)
    return {"suggestion": result.model_dump(), "note": "This is a preview only - nothing has been applied."}


# ---- Action tools (never auto-executed - see routers/agent.py's pendingAction gate) ---------


async def send_message(args: dict, token: str | None = None) -> dict:
    return await internal_request(
        "POST",
        "/messages",
        body={
            "subjectType": args.get("subjectType"),
            "subjectId": args.get("subjectId"),
            "channel": args.get("channel"),
            "content": args.get("content"),
        },
        token=token,
    )


async def acknowledge_alert(args: dict, token: str | None = None) -> dict:
    return await internal_request("POST", f"/alerts/{args['alertId']}/acknowledge", token=token)


READ_TOOLS = {
    "get_vehicle_status": get_vehicle_status,
    "get_active_alerts": get_active_alerts,
    "get_order_status": get_order_status,
    "suggest_reroute": suggest_reroute,
}

ACTION_TOOLS = {
    "send_message": send_message,
    "acknowledge_alert": acknowledge_alert,
}

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "name": "get_vehicle_status",
        "description": "Look up a vehicle's current status by its plate number or database id.",
        "parameters": {
            "type": "object",
            "properties": {"plateOrId": {"type": "string", "description": 'Plate number (e.g. "DCD8953") or numeric id'}},
            "required": ["plateOrId"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_active_alerts",
        "description": "List currently open alerts, optionally filtered by severity.",
        "parameters": {
            "type": "object",
            "properties": {"severity": {"type": "string", "enum": ["Critical", "Warning", "Info"], "description": "Optional severity filter"}},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_order_status",
        "description": "Look up an order's current status by its database id.",
        "parameters": {
            "type": "object",
            "properties": {"orderId": {"type": "string", "description": "Numeric id of the order"}},
            "required": ["orderId"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "suggest_reroute",
        "description": "Preview an optimized stop sequence for a vehicle's current route. Read-only - never applies the change.",
        "parameters": {
            "type": "object",
            "properties": {"vehicleId": {"type": "string", "description": "Numeric id of the vehicle"}},
            "required": ["vehicleId"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "send_message",
        "description": "Send an operational message over a communication channel. Requires human confirmation before it executes.",
        "parameters": {
            "type": "object",
            "properties": {
                "subjectType": {"type": "string", "enum": ["order", "route", "vehicle", "user"]},
                "subjectId": {"type": "string"},
                "channel": {"type": "string", "enum": ["WhatsApp", "Viber", "SMS", "Voice AI"]},
                "content": {"type": "string"},
            },
            "required": ["subjectType", "subjectId", "channel", "content"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "acknowledge_alert",
        "description": "Acknowledge an open alert. Requires human confirmation before it executes.",
        "parameters": {
            "type": "object",
            "properties": {"alertId": {"type": "string", "description": "Numeric id of the alert"}},
            "required": ["alertId"],
            "additionalProperties": False,
        },
    },
]
