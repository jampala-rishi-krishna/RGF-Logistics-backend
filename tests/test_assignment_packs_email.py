"""Assignment email (driver, team, preview all share one table): packs only from pack-unit lines, partial kg never shown as complete."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from routers import assignment
from services import vapi_client, weight_display


def order(number, items, address="1 Main St"):
    return SimpleNamespace(id=number, salesorder_number=number, customer_name=f"Client {number}", shipping_address=None,
                           raw_json={"line_items": items, "shipping_address": {"address": address, "city": "Makati"}})


def line(unit, quantity, item_id=None):
    return {"unit": unit, "quantity": quantity, "item_id": item_id, "name": "x"}


PROFILE = SimpleNamespace(plate_no="DCD8954", capacity_note=None)
DRIVERS = [SimpleNamespace(name="Albert", warehouse="METS", email="a@example.test")]


def html_for(orders, weight=lambda o: 100.0, unknown=lambda o: 0):
    return assignment._assignment_email_html(DRIVERS, PROFILE, orders, "Ops", "2026-10-13T00:00:00+00:00", weight_fn=weight, unknown_fn=unknown)


def cells(html, number):
    row = html[html.index(f">{number}</td>"):]
    row = row[: row.index("</tr>")]
    return [c.split(">", 1)[1] for c in row.split("</td>") if ">" in c]


def test_packs_count_only_pck_pack_packs_lines_kg_and_pc_are_not_packs():
    o = order("SO-1", [line("kg", 50), line("pck", 36), line("pc", 5), line("packs", 4)])
    assert assignment._order_packs(o) == 40.0
    _, html, _ = html_for([o])
    assert cells(html, "SO-1")[5] == "40"


def test_an_order_without_pack_lines_shows_a_dash_not_a_quantity_sum():
    o = order("SO-2", [line("kg", 564), line("pc", 12)])
    assert assignment._order_packs(o) is None
    _, html, body = html_for([o])
    assert cells(html, "SO-2")[5] == "\u2014" and cells(body, "SO-2")[5] == "\u2014"  # driver email, team email and preview all use this same table
    text = assignment._assignment_email_text(PROFILE, [o], weight_fn=lambda x: 1.0, unknown_fn=lambda x: 0)
    assert "Total Packs: \u2014" in text


def test_partial_weight_is_written_with_a_plus_and_a_note_never_as_complete():
    o = order("SO-3", [line("kg", 354), line("pck", 36, "no-weight-item")])
    _, html, _ = html_for([o], weight=lambda x: 354.0, unknown=lambda x: 1)
    kg_cell = cells(html, "SO-3")[4]
    assert "354.0 kg+" in kg_cell and "1 line without weight" in kg_cell
    text = assignment._assignment_email_text(PROFILE, [o], weight_fn=lambda x: 354.0, unknown_fn=lambda x: 1)
    assert "Total kg: 354.0 kg+ (1 line without weight)" in text
    _, html2, _ = html_for([order("SO-4", [line("kg", 10)])], weight=lambda x: 10.0, unknown=lambda x: 0)
    assert "kg+" not in html2 and "without weight" not in html2 and cells(html2, "SO-4")[4] == "10.0"  # complete stays plain


def test_unknown_weight_counted_from_items_already_in_memory(monkeypatch):
    o = order("SO-5", [line("kg", 5), line("pck", 6, "item-a"), line("pc", 2, None)])
    monkeypatch.setattr(assignment, "calculate_line_weight_kg", lambda quantity, unit, item_id, **kw: float(quantity) if str(unit).lower() == "kg" else None)
    assert assignment._unknown_weight_lines(o) == 2


@pytest.mark.parametrize("kg,unknown,text,note", [
    (354.0, 0, "354.0 kg", None), (354.0, 1, "354.0 kg+", "1 line without weight"), (354.0, 3, "354.0 kg+", "3 lines without weight"), (0.0, 2, "\u2014", "2 lines without weight"), (None, 0, "\u2014", None),
])
def test_format_kg(kg, unknown, text, note):
    assert weight_display.format_kg(kg, unknown) == (text, note)


def test_voice_text_has_no_zero_packs_and_says_at_least_for_partial_weight():
    said = vapi_client.so_details_text([{"soNumber": "SO-1", "clientName": "Acme", "totalKgs": 354.0, "totalKgsIncomplete": True, "totalPacks": None}])
    assert "at least 354.0 kilograms" in said and "packs" not in said
    assert ", 6 packs" in vapi_client.so_details_text([{"soNumber": "SO-1", "clientName": "Acme", "totalKgs": 5, "totalPacks": 6}])
