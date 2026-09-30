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
    shared_subject = f"Sales orders{f' for {customer}' if customer else ''} | {date_from} to {date_to}"
    return {"subject": shared_subject, "htmlBody": render_rarechain_email("COLD-CHAIN OPERATIONS / PHILIPPINES", "Sales orders are ready.", "Your filtered Load Planning view is attached for review and next-mile coordination.", "https://images.pexels.com/photos/7464230/pexels-photo-7464230.jpeg?auto=compress&cs=tinysrgb&w=1200", [{"label": "Orders", "value": order_count}, {"label": "Date range", "value": f"{date_from} - {date_to}"}, {"label": "Status", "value": status}], "https://images.pexels.com/photos/6169056/pexels-photo-6169056.jpeg?auto=compress&cs=tinysrgb&w=1800", "Connected operations across the network.", shared_body)}
    customer = _email_text(orders[0].get("customerName")) if len(orders) == 1 and isinstance(orders[0], dict) else ""
    subject = f"Sales orders{f' for {customer}' if customer else ''} | {date_from} to {date_to}"
    html_body = f'''<div style="margin:0;background:#FAFAF8;color:#0B0B0B;font-family:'DM Sans',Arial,sans-serif;line-height:1.5;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#FAFAF8;padding:28px 12px;">
    <tr><td align="center">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:100%;max-width:640px;background:#FFFFFF;border:1px solid #E4E3DF;">
        <tr><td style="background:#0B0B0B;padding:18px 28px;color:#FFFFFF;font-family:'Space Grotesk','DM Sans',Arial,sans-serif;font-size:20px;font-weight:700;letter-spacing:-.03em;">RARECHAIN<span style="color:#A1A1A1;">.</span></td></tr>
        <tr><td style="padding:28px 28px 12px;"><div style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#77787B;">COLD-CHAIN OPERATIONS / PHILIPPINES</div><h1 style="margin:12px 0 10px;font-family:'Space Grotesk','DM Sans',Arial,sans-serif;font-size:32px;line-height:1.08;letter-spacing:-.04em;color:#0B0B0B;">Sales orders are ready.</h1><p style="margin:0;color:#55565A;font-size:15px;">Your filtered Load Planning view is attached for review and next-mile coordination.</p></td></tr>
        <tr><td style="padding:12px 28px 24px;"><img src="https://images.pexels.com/photos/7464230/pexels-photo-7464230.jpeg?auto=compress&amp;cs=tinysrgb&amp;w=1200" alt="RareChain logistics operations" width="584" style="display:block;width:100%;height:auto;max-height:280px;object-fit:cover;border:1px solid #E4E3DF;" /></td></tr>
        <tr><td style="padding:0 28px 24px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #E4E3DF;border-bottom:1px solid #E4E3DF;"><tr><td style="padding:14px 0;width:33%;"><div style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#77787B;">Orders</div><strong style="display:block;margin-top:4px;font-size:20px;color:#0B0B0B;">{order_count}</strong></td><td style="padding:14px 0;width:33%;"><div style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#77787B;">Date range</div><strong style="display:block;margin-top:4px;font-size:13px;color:#0B0B0B;">{date_from} - {date_to}</strong></td><td style="padding:14px 0;width:33%;"><div style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#77787B;">Status</div><strong style="display:block;margin-top:4px;font-size:13px;color:#1E7B44;">{status}</strong></td></tr></table></td></tr>
        <tr><td style="padding:0 28px 24px;"><img src="https://images.pexels.com/photos/6169056/pexels-photo-6169056.jpeg?auto=compress&amp;cs=tinysrgb&amp;w=1800" alt="Connected warehouse operations" width="584" style="display:block;width:100%;height:auto;max-height:220px;object-fit:cover;border:1px solid #E4E3DF;" /><div style="padding-top:8px;font-size:11px;color:#77787B;">Connected operations across the network.</div></td></tr>
        <tr><td style="padding:0 28px 28px;"><p style="margin:0;color:#55565A;font-size:14px;line-height:1.65;">Please review the attached sales-order documents before coordinating the next handoff. The PDF is formatted for sharing; the Excel file contains the operational rows for further planning.</p></td></tr>
        <tr><td style="padding:20px 28px;border-top:1px solid #E4E3DF;background:#FAFAF8;color:#55565A;font-size:13px;">Warm regards,<br /><strong style="color:#0B0B0B;">Martin Reyes</strong><br />RareChain Logistics Team<br />Rare Global Food Trading Corp.<br />Unit SF02 Santana Grove, Soreena Avenue corner Dr. A. Santos Avenue<br />San Antonio Paranaque City, Manila, Philippines<br />Mobile: +63 9171145694<br /><a href="mailto:martin@rareglobalfood.com" style="color:#0B0B0B;">martin@rareglobalfood.com</a> · <a href="http://www.rareglobalfood.com" style="color:#0B0B0B;">www.rareglobalfood.com</a></td></tr>
      </table>
    </td></tr>
  </table>
</div>'''
    return {"subject": subject, "htmlBody": html_body}
