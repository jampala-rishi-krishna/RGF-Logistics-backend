"""READ-ONLY audit of where inbound WhatsApp replies for the logistics sender are routed.

    python scripts/twilio_webhook_audit.py

Only issues GET requests; it never changes a sender, service or webhook. Needs
TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET in backend/.env (never printed).
"""
from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

NUMBER = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+639171145694")
LOGISTICS_HOOK = "intellifleet-logistics-whatsapp-incoming"  # n8n workflow rhd4FYmpJoEhqg7V
MESSAGING = "https://messaging.twilio.com"


def get(client: httpx.Client, url: str, **params):
    response = client.get(url, params=params or None)
    if response.status_code != 200:
        raise SystemExit(f"GET {url} -> HTTP {response.status_code}: {response.json().get('message', '') if response.headers.get('content-type', '').startswith('application/json') else ''}")
    return response.json()


def classify(url: str | None) -> str:
    if not url:
        return "(none)"
    if LOGISTICS_HOOK in url:
        return "LOGISTICS workflow"
    return "NOT the logistics webhook (sales or other)"


def main() -> None:
    key_sid, secret = os.environ.get("TWILIO_API_KEY_SID", ""), os.environ.get("TWILIO_API_KEY_SECRET", "")
    if not (key_sid and secret and os.environ.get("TWILIO_ACCOUNT_SID")):
        raise SystemExit("Missing TWILIO_ACCOUNT_SID / TWILIO_API_KEY_SID / TWILIO_API_KEY_SECRET.")
    with httpx.Client(auth=(key_sid, secret), timeout=30) as client:
        senders, url = [], f"{MESSAGING}/v2/Channels/Senders"
        params = {"Channel": "whatsapp", "PageSize": 50}
        while url:
            page = get(client, url, **params)
            senders += page.get("senders") or []
            nxt = (page.get("meta") or {}).get("next_page_url")
            url, params = nxt, {}
        mine = [s for s in senders if (s.get("sender_id") or "").replace(" ", "") == NUMBER]
        print(f"WhatsApp senders on account: {len(senders)}; matching {NUMBER}: {len(mine)}")
        for sender in mine:
            hook = sender.get("webhook") or {}
            print(f"  sender status        : {sender.get('status')}")
            print(f"  inbound webhook URL  : {hook.get('callback_url')}  [{hook.get('callback_method')}]  -> {classify(hook.get('callback_url'))}")
            print(f"  fallback URL         : {hook.get('fallback_url')}  [{hook.get('fallback_method')}]")
            print(f"  status callback URL  : {hook.get('status_callback_url') or hook.get('status_callback_method') and None}")
            print(f"  sender sid           : {sender.get('sid')}")
        services = get(client, f"{MESSAGING}/v1/Services", PageSize=100).get("services") or []
        print(f"\nMessaging Services: {len(services)}")
        owner = None
        for service in services:
            senders_in = get(client, f"{MESSAGING}/v1/Services/{service['sid']}/ChannelSenders", PageSize=100).get("senders") or []
            numbers = [(s.get("sender_id") or "") for s in senders_in]
            if NUMBER in numbers:
                owner = service
                print(f"  {NUMBER} belongs to service '{service.get('friendly_name')}' ({service['sid']})")
                print(f"  use_inbound_webhook_on_number: {service.get('use_inbound_webhook_on_number')}")
                print(f"  service inbound_request_url   : {service.get('inbound_request_url')}  -> {classify(service.get('inbound_request_url'))}")
                print(f"  service fallback_url          : {service.get('fallback_url')}")
                print(f"  service status_callback       : {service.get('status_callback')}")
        if owner is None:
            print(f"  {NUMBER} is not attached to any Messaging Service; the sender's own webhook applies.")
        effective = None
        if mine:
            effective = (mine[0].get("webhook") or {}).get("callback_url")
        if owner and not owner.get("use_inbound_webhook_on_number"):
            effective = owner.get("inbound_request_url")
        print(f"\nEFFECTIVE inbound receiver for ALL replies on {NUMBER}: {effective}  => {classify(effective)}")


if __name__ == "__main__":
    sys.exit(main())
