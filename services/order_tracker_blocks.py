"""Structured Ask answer blocks, built ONLY from the read-only tool results of one request.

Pure functions: no IO, no module-level mutable state. Numbers come from the tools,
never from model text. Every string passes through tracker.safe_text.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote

from services import order_tracker as tracker

MAX_BLOCKS, MAX_ROWS, MAX_BARS, MAX_NOT_RECORDED, MAX_BYTES = 5, 20, 8, 2, 12000
LINK_PREFIX = "/app/order-tracker"
TAGALOG = frozenset("ba na ang ng nasaan bayad ilan sino kailan hindi mga po saan anong ano ito iyan yung nang sa naman pa".split())
NOT_RECORDED_PREFIX = re.compile(r"^\s*not recorded\s*[-–—:]\s*", re.I)
PERMIT = re.compile(r"\bpermi(?:t|ts|so)\b", re.I)
PO_COPY = re.compile(r"\bpo\s*copy\b|\bcopy of (?:the )?(?:client )?po\b|\bkopya ng po\b", re.I)

LABELS = {
    "en": {"invoice": "Invoice", "paid_on": "Paid on", "matches": "Matches invoice?", "yes": "Yes", "no": "No",
           "total": "Total", "paid_amount": "Paid", "balance": "Balance", "Paid": "Paid", "Partly paid": "Partly paid",
           "Unpaid": "Unpaid", "not_recorded_value": "Not recorded", "nr_title": "Not recorded.",
           "bars_title": "Top SKUs by number of buyers", "bars_unit": "buyers", "open": "Open ", "photos": "See the photos"},
    "tl": {"invoice": "Invoice", "paid_on": "Binayaran", "matches": "Tugma sa invoice?", "yes": "Oo, tugma", "no": "Hindi tugma",
           "total": "Kabuuan", "paid_amount": "Nabayaran", "balance": "Natitira", "Paid": "Bayad na", "Partly paid": "Bahagyang bayad",
           "Unpaid": "Hindi pa bayad", "not_recorded_value": "Hindi naka-record", "nr_title": "Hindi naka-record.",
           "bars_title": "Mga nangungunang SKU ayon sa dami ng mamimili", "bars_unit": "mamimili",
           "open": "Buksan ang ", "photos": "Tingnan ang mga larawan"},
}
CHIP_TONES = {"Paid": "positive", "Partly paid": "warning", "Unpaid": "neutral"}
SECTION_TOOLS = {"truck_assignment", "truck_location", "order_times", "delivery_result", "proof_of_delivery", "payment_status",
                 "order_documents", "loading_check", "order_issues"}


def detect_language(question) -> str:
    words = re.findall(r"[a-z]+", str(question or "").casefold())
    return "tl" if any(w in TAGALOG for w in words) else "en"


def _text(value, limit=300):
    return tracker.safe_text(value, limit) if value is not None else None


def _link(value):
    link = _text(value, 500)
    return link if isinstance(link, str) and link.startswith(LINK_PREFIX) else None


def _peso(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite():
        return None
    if amount == amount.to_integral_value():
        return "₱" + f"{int(amount):,}"
    return "₱" + f"{amount.quantize(Decimal('0.01')):,}"


def _clean_missing(message):
    text = NOT_RECORDED_PREFIX.sub("", str(message or "")).strip()
    return text[:1].upper() + text[1:] if text else "Ask Logistics."


def rejected_summary(value):
    if not isinstance(value, list) or not value:
        return None
    parts = []
    for item in value[:3]:
        if isinstance(item, dict) and item.get("sku") and item.get("unit") and item.get("quantity") is not None:
            parts.append(f"{item['quantity']} {item['unit']} {item['sku']}")
    if not parts:
        return None
    extra = len(value) - 3
    return ", ".join(parts) + (f" +{extra} more" if extra > 0 else "")


def _card_row(card):
    sections = card.get("sections") or {}
    result = (sections.get("delivery_result") or {}).get("value") or {}
    return {"number": card.get("number"), "customer": card.get("customer") or "", "status": card.get("status") or "",
            "reason": result.get("reason") if isinstance(result, dict) else None,
            "rejected": rejected_summary(result.get("rejected_items")) if isinstance(result, dict) else None,
            "link": card.get("link")}


def _delivery_row(result):
    value = result.get("value") if isinstance(result.get("value"), dict) else {}
    recorded = result.get("state") == "recorded"
    return {"number": result.get("order"), "customer": "",
            "status": (value.get("outcome") if recorded else None) or "Not recorded",
            "reason": value.get("reason"), "rejected": rejected_summary(value.get("rejected_items")), "link": result.get("link")}


def _tiles(result, words):
    value = result.get("value") if isinstance(result.get("value"), dict) else {}
    status = value.get("status")
    if result.get("state") != "recorded" or not status or status == "Unknown":
        return None
    date, ref = value.get("payment_date"), value.get("payment_reference")
    paid_on = (str(date) + (" · " + str(ref) if ref else "")) if date else words["not_recorded_value"]
    matches = value.get("arithmetic") == "matches"
    tiles = [{"label": words["invoice"], "value": value.get("invoice_number") or words["not_recorded_value"], "tone": None},
             {"label": words["paid_on"], "value": paid_on, "tone": None},
             {"label": words["matches"], "value": words["yes"] if matches else words["no"], "tone": "positive" if matches else "warning"}]
    for key, label in (("invoice_total", "total"), ("paid", "paid_amount"), ("balance", "balance")):
        amount = _peso(value.get(key))
        if amount:
            tiles.append({"label": words[label], "value": amount, "tone": None})
    return {"type": "tiles", "tiles": tiles,
            "chip": {"label": words.get(status, status), "tone": CHIP_TONES.get(status, "unknown")},
            "source": result.get("source") or "", "as_of": result.get("as_of")}


def _bars(result, words):
    items = [i for i in result.get("items") or [] if isinstance(i, dict) and isinstance(i.get("buyers"), int)]
    if not items:
        return None
    items.sort(key=lambda i: (-i["buyers"], str(i.get("sku")), str(i.get("unit"))))
    stamps = [i.get("as_of") for i in items if i.get("as_of")]
    return {"type": "bars", "title": words["bars_title"], "unit": words["bars_unit"],
            "items": [{"label": f"{i['name']} ({i.get('sku')})" if i.get("name") else str(i.get("sku")), "value": i["buyers"]} for i in items[:MAX_BARS]],
            "source": items[0].get("source") or "Zoho cached SO line_items", "as_of": min(stamps) if stamps else None}


def _sanitize(value, key=None):
    if isinstance(value, dict):
        return {k: _sanitize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, str) and key not in {"type", "kind", "tone"}:
        return _link(value) if key == "link" else _text(value)
    return value


def build_blocks(results, language, question=""):
    """results: [(tool_name, tool_result)] executed successfully in this request."""
    words = LABELS["en"]
    rows, table_meta, tiles, bars, missing = {}, None, [], None, {}
    photo_links, single_links = [], []

    def add_row(row, source, as_of):
        nonlocal table_meta
        number = row.get("number")
        if not number:
            return
        if number in rows:
            for key, val in row.items():
                if rows[number].get(key) in (None, "", "Not recorded") and val not in (None, ""):
                    rows[number][key] = val
        elif len(rows) < MAX_ROWS:
            rows[number] = dict(row)
        if table_meta is None:
            table_meta = (source or "", as_of)

    def add_missing(message):
        text = _clean_missing(message)
        if text not in missing:
            missing[text] = {"type": "not_recorded", "title": words["nr_title"], "message": text}

    def check_documents(value):
        if not isinstance(value, dict):
            return
        for pattern, key in ((PERMIT, "permits"), (PO_COPY, "client_po_copy")):
            field = value.get(key)
            if pattern.search(question or "") and isinstance(field, str) and NOT_RECORDED_PREFIX.match(field):
                add_missing(field)

    for name, result in results or []:
        if not isinstance(result, dict):
            continue
        if name == "find_orders":
            for order in result.get("orders") or []:
                if isinstance(order, dict):
                    add_row({"number": order.get("number"), "customer": order.get("customer") or "", "status": order.get("status") or "",
                             "reason": None, "rejected": None, "link": order.get("link")}, result.get("source"), result.get("as_of"))
        elif name == "delivery_summary":
            for order in result.get("orders") or []:
                add_row({"number": order.get("number"), "customer": order.get("customer") or "", "status": order.get("status") or "",
                         "reason": order.get("reason"), "rejected": rejected_summary(order.get("rejected_items")),
                         "link": order.get("link")}, result.get("source"), result.get("as_of"))
        elif name == "get_order":
            sections = result.get("sections") or {}
            status_now = sections.get("status_now") or {}
            add_row(_card_row(result), status_now.get("source"), status_now.get("as_of"))
            single_links.append(result.get("link"))
            if ((sections.get("proof") or {}).get("value") or {}).get("photos"):
                photo_links.append(result.get("link"))
            check_documents((sections.get("documents") or {}).get("value"))
        elif name == "buyers_per_sku":
            bars = bars or _bars(result, words)
            if bars is None and result.get("message"):
                add_missing(result["message"])
        elif name in SECTION_TOOLS:
            single_links.append(result.get("link"))
            if name == "delivery_result":
                add_row(_delivery_row(result), result.get("source"), result.get("as_of"))
            if name == "payment_status":
                block = _tiles(result, words)
                if block:
                    tiles.append(block)
            if name == "proof_of_delivery" and ((result.get("value") or {}).get("photos")):
                photo_links.append(result.get("link"))
            if name == "order_documents":
                check_documents(result.get("value"))
            if result.get("state") == "not_recorded" and result.get("message"):
                add_missing(result["message"])

    blocks = []
    if rows:
        blocks.append({"type": "order_table", "rows": list(rows.values()), "source": table_meta[0], "as_of": table_meta[1]})
    blocks.extend(tiles[:2])
    if bars:
        blocks.append(bars)
    blocks.extend(list(missing.values())[:MAX_NOT_RECORDED])

    actions, seen = [], set()
    for link in photo_links:
        link = _link(link)
        if link and link + "#proof" not in seen:
            seen.add(link + "#proof")
            actions.append({"kind": "photos", "label": words["photos"], "link": link + "#proof"})
    open_links = [(r["number"], r.get("link")) for r in rows.values()] + [(None, link) for link in single_links]
    opened = 0
    for number, link in open_links:
        link = _link(link)
        if not link or link in seen or opened >= 2:
            continue
        seen.add(link)
        opened += 1
        label_number = number
        if label_number is None:
            match = re.search(r"order=([^&#]+)", link)
            label_number = unquote(match.group(1)) if match else "order"
        actions.append({"kind": "open_order", "label": words["open"] + str(label_number), "link": link})

    content_cap = MAX_BLOCKS - (1 if actions else 0)
    blocks = blocks[:content_cap]
    if actions:
        blocks.append({"type": "actions", "actions": actions[:4]})
    blocks = _sanitize(blocks)
    while blocks and len(json.dumps(blocks, ensure_ascii=False).encode()) > MAX_BYTES:
        table = next((b for b in blocks if b["type"] == "order_table" and len(b["rows"]) > 1), None)
        if table:
            table["rows"] = table["rows"][: max(1, len(table["rows"]) // 2)]
        else:
            content = [i for i, b in enumerate(blocks) if b["type"] != "actions"]
            blocks.pop(content[-1] if content else -1)
    return blocks
