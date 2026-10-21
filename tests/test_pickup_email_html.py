"""Pickup list email uses the same RareChain HTML layout as the crew assignment email."""
from __future__ import annotations

from services import pickup_email

ROW = {"po": "PO-00123", "vendor": "Vendor <A>", "address": "1 Main St, Makati", "contact": "Ana 0917", "delivery_date": "2026-10-23",
       "destination": "METS", "vendor_address_missing": False,
       "items": [{"name": "Chicken", "qty": 5, "unit": "pack", "kg": 12.419999999}, {"name": "Beef", "qty": 2, "unit": "box", "kg": None}]}


def data(scheduled, pahabol=()):
    return {"delivery_date": "2026-10-23", "scheduled": list(scheduled), "pahabol": list(pahabol)}


def test_pickup_email_uses_rarechain_layout_with_a_table_and_stats():
    html = pickup_email.email_html(data([ROW]))
    assert "RARECHAIN" in html and "Warm regards" in html  # same shell as the assignment email
    assert all(f">{h}</th>" in html for h in ("PO#", "Vendor", "Pickup address", "Contact / phone", "Destination warehouse", "Items", "Total kg"))
    assert "SCHEDULED POs" in html and "PAHABOL POs" in html and "PICKUP DATE" in html
    assert "Vendor &lt;A&gt;" in html and "<A>" not in html  # escaped
    assert "12.42 kg+" in html and "1 line(s) without weight" in html and "kg unknown" in html  # rounded, partial weight flagged
    assert "Not scheduled – needs Logistics approval (0)" in html and "None." in html


def test_missing_vendor_address_and_contact_are_flagged_red():
    html = pickup_email.email_html(data([], [{**ROW, "address": "", "contact": "", "vendor_address_missing": True}]))
    assert "Vendor address missing" in html and "#b42318" in html and ">Missing</span>" in html
