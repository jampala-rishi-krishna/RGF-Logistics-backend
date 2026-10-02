from __future__ import annotations

import os
import json
from pathlib import Path

from dotenv import load_dotenv
from fastapi import HTTPException
from openai import AsyncOpenAI

from services.agent_tools import TOOL_DEFINITIONS
from services.rarechain_email_template import render_rarechain_email

# Resolve the backend environment independently of the shell's current directory.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

# Uses OpenAI's Responses API (client.responses.create) - ported verbatim from
# ai-agent-module/lib/openai-client.js. Tools are declared flattened
# ({type:"function", name, description, parameters}), matching the Responses API's contract.
AGENT_INSTRUCTIONS = """
You are Martin Reyes, the IntelliFleet logistics operations agent for Rare Global Food Trading.
Welcome the dispatcher warmly when appropriate. Be concise, practical, and grounded in live data.
Use the available tools whenever a question requires current fleet, alert, order, or route information;
never invent operational facts. For actions, explain what you intend to do and let the application
request human confirmation before execution. After a tool result, summarize what changed and include
the relevant vehicle plate, speed, fuel, ignition, location, alert, or order details.
""".strip()


def _client() -> AsyncOpenAI:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(501, "OPENAI_API_KEY is not configured.")
    return AsyncOpenAI(api_key=api_key)


async def create_response(input_items: list[dict]):
    model = os.environ.get("OPENAI_MODEL") or "gpt-4o"
    client = _client()
    return await client.responses.create(
        model=model,
        instructions=AGENT_INSTRUCTIONS,
        tools=TOOL_DEFINITIONS,
        input=input_items,
    )


def extract_text(response) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text:
        return text
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) == "message":
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "output_text":
                    return part.text
    return ""


def _email_text(value) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


async def generate_sales_order_email_draft(context: dict) -> dict[str, str]:
    """Return the fixed RareChain email template used for every sales-order send."""
    date_from = _email_text(context.get("dateFrom") or "the selected period")
    date_to = _email_text(context.get("dateTo") or date_from)
    status = _email_text(context.get("status") or "All")
    order_count = _email_text(context.get("orderCount") or 0)
    orders = context.get("orders") or []
    shared_body = "<p style=\"margin:0;\">Please review the attached sales-order documents before coordinating the next handoff. The PDF is formatted for sharing; the Excel file contains the operational rows for further planning.</p>"
    customer = _email_text(orders[0].get("customerName")) if len(orders) == 1 and isinstance(orders[0], dict) else ""
    shared_subject = f"Sales orders{f' for {customer}' if customer else ''} | {date_from} to {date_to}"
    return {"subject": shared_subject, "htmlBody": render_rarechain_email("COLD-CHAIN OPERATIONS / PHILIPPINES", "Sales orders are ready.", "Your filtered Load Planning view is attached for review and next-mile coordination.", "https://images.pexels.com/photos/7464230/pexels-photo-7464230.jpeg?auto=compress&cs=tinysrgb&w=1200", [{"label": "Orders", "value": order_count}, {"label": "Date range", "value": f"{date_from} - {date_to}"}, {"label": "Status", "value": status}], "https://images.pexels.com/photos/6169056/pexels-photo-6169056.jpeg?auto=compress&cs=tinysrgb&w=1800", "Connected operations across the network.", shared_body)}
