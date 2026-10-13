"""Confirmed-orders warehouse email: warehouse filtering, recipient validation, human-in-the-loop send. Mocked: no Zoho, no Gmail."""
from __future__ import annotations

import base64
import os
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost")

from auth.dependencies import CurrentUser, get_current_user  # noqa: E402
from database import get_db  # noqa: E402
from routers import confirmed_email as router_mod, load_planning  # noqa: E402
from services import confirmed_email as svc, gmail_sender, live_sales_order_cache as lc  # noqa: E402

METS, GLACIER = "Mets Cold Storage", "Glacier Cold Storage"


def line(name, wh, item_id=None):
    return {"item_id": item_id or name, "name": name, "sku": name, "quantity": 1, "unit": "kg", "warehouse_name": wh}


def order(oid, number, lines, **extra):
    return SimpleNamespace(id=oid, salesorder_number=number, customer_name="Cust", shipping_address=None, expected_shipment_date=date(2026, 10, 14), order_status="confirmed", total=1,
                           vehicle_id="T1", driver_id=None, helper_ids=[], raw_json={"line_items": lines, "shipping_address": {"address": "1 Road", "city": "Manila"}}, **extra)


ORDERS = {
    "m": order("m", "SO-M", [line("Egg", METS)]),
    "g": order("g", "SO-G", [line("Ice", GLACIER)]),
    "x": order("x", "SO-X", [line("Milk", METS), line("Fish", GLACIER)]),
    "t": order("t", "TO-1", [line("Pork", METS)], order_type="TO"),
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(lc, "find_cached", lambda oid: ORDERS.get(oid))
    monkeypatch.setattr(lc, "ensure_zoho_data", lambda oid: None)
    monkeypatch.setattr(load_planning, "_hydrate_export_rows", lambda db, rows: rows)
    monkeypatch.setattr(load_planning, "fetch_item_stock", lambda item_id, branch: {"mets": 5, "glacier": 6})
    monkeypatch.setattr(load_planning.branch_service, "branch_id_of", lambda o: "b")
    monkeypatch.setattr(load_planning.staff_directory_cache, "get_by_id", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=1, email="p@x.com", role="dispatcher", full_name="P", status="active")
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app)


def numbers(data):
    return {row[5] for row in data["rows"]}


def test_mets_preview_excludes_glacier_and_keeps_mixed_mets_lines(client):
    data = client.post("/api/load-planning/email/confirmed/preview", json={"warehouse": "mets", "order_ids": list(ORDERS)}).json()
    assert numbers(data) == {"SO-M", "SO-X", "TO-1"}
    assert {row[1] for row in data["rows"]} == {"Egg", "Milk", "Pork"}
    assert data["order_count"] == 3 and data["line_count"] == 3 and data["counts_by_type"] == {"SO": 2, "TO": 1}
    assert data["headers"][6] == "Type" and data["rows"][0][6] in ("SO", "TO")
    assert base64.b64decode(data["pdf_base64"]).startswith(b"%PDF") and base64.b64decode(data["xlsx_base64"]).startswith(b"PK")
    assert data["pdf_filename"].endswith(".pdf") and data["xlsx_filename"].endswith(".xlsx")


def test_glacier_preview_only_glacier_lines(client):
    data = client.post("/api/load-planning/email/confirmed/preview", json={"warehouse": "glacier", "order_ids": ["m", "g", "x", "t"]}).json()
    assert numbers(data) == {"SO-G", "SO-X"} and {row[1] for row in data["rows"]} == {"Ice", "Fish"}


def test_zero_line_orders_dropped_and_empty_is_422(client):
    assert numbers(client.post("/api/load-planning/email/confirmed/preview", json={"warehouse": "glacier", "order_ids": ["m", "g"]}).json()) == {"SO-G"}
    assert client.post("/api/load-planning/email/confirmed/preview", json={"warehouse": "glacier", "order_ids": ["m"]}).status_code == 422
    assert client.post("/api/load-planning/email/confirmed/preview", json={"warehouse": "mets", "order_ids": []}).status_code == 422


@pytest.mark.parametrize("wh", [None, "", "both", "METS ", "cebu"])
def test_warehouse_required_and_validated(client, wh):
    status = client.post("/api/load-planning/email/confirmed/preview", json={"warehouse": wh, "order_ids": ["m"]}).status_code
    assert status == (200 if wh == "METS " else 422)


def test_filter_unit_does_not_mutate_original():
    kept = svc.filter_orders_for_warehouse([ORDERS["x"]], "mets")
    assert len(kept) == 1 and [i["name"] for i in kept[0].raw_json["line_items"]] == ["Milk"]
    assert len(ORDERS["x"].raw_json["line_items"]) == 2 and kept[0].salesorder_number == "SO-X"


def test_recipient_normalisation():
    assert svc.normalize_recipients("a@x.com; b@x.com, A@X.com", "to") == ["a@x.com", "b@x.com"]
    assert svc.normalize_recipients(["a@x.com", "b@x.com;c@x.com"], "to") == ["a@x.com", "b@x.com", "c@x.com"]
    assert svc.normalize_recipients(None, "cc") == []
    with pytest.raises(svc.ConfirmedEmailError, match="nope"):
        svc.normalize_recipients(["a@x.com", "nope"], "to")
    for bad in ("a@b", "a b@x.com", "@x.com", "a@@x.com", "a..b@x.com", "a@x..com"):
        with pytest.raises(svc.ConfirmedEmailError):
            svc.normalize_recipients(bad, "to")
    with pytest.raises(svc.ConfirmedEmailError):
        svc.normalize_recipients([], "to", required=True)
    with pytest.raises(svc.ConfirmedEmailError, match="Too many"):
        svc.normalize_recipients([f"u{i}@x.com" for i in range(26)], "cc")


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(gmail_sender, "configured", lambda: True)

    def fake(**kwargs):
        calls.append(kwargs)
        return {"id": "m1", "threadId": "t1", "to": kwargs["to"]}

    monkeypatch.setattr(gmail_sender, "send_email", fake)
    return calls


def body(**over):
    return {"warehouse": "mets", "order_ids": ["m", "x"], "to": ["w@x.com"], "cc": [], "subject": "Confirmed", "html_body": "<p>hi</p>", **over}


def test_preview_never_sends(client, sent):
    client.post("/api/load-planning/email/confirmed/preview", json={"warehouse": "mets", "order_ids": ["m"]})
    assert sent == []


def test_send_regenerates_files_server_side_and_dedupes_cc(client, sent):
    res = client.post("/api/load-planning/email/confirmed/send", json=body(to=["a@x.com", "A@x.com"], cc="b@x.com; a@x.com", pdf_base64="IGNORED"))
    assert res.status_code == 200, res.text
    assert res.json() == {"success": True, "status": "sent", "messageId": "m1", "orderCount": 2, "lineCount": 2}
    assert len(sent) == 1 and sent[0]["to"] == ["a@x.com"] and sent[0]["cc"] == ["b@x.com"]
    names = [a[0] for a in sent[0]["attachments"]]
    assert names[0].endswith(".pdf") and names[1].endswith(".xlsx") and sent[0]["attachments"][0][2].startswith(b"%PDF")


@pytest.mark.parametrize("over", [{"to": []}, {"to": ""}, {"to": ["bad"]}, {"cc": ["ok@x.com", "bad@"]}, {"subject": " "}, {"warehouse": "x"}])
def test_send_validation_errors_do_not_send(client, sent, over):
    assert client.post("/api/load-planning/email/confirmed/send", json=body(**over)).status_code == 422
    assert sent == []


def test_invalid_recipient_named_in_error(client, sent):
    detail = client.post("/api/load-planning/email/confirmed/send", json=body(to=["good@x.com", "bad@@x"])).json()["detail"]
    assert "bad@@x" in detail


def test_send_503_when_gmail_not_configured(client, monkeypatch):
    monkeypatch.setattr(gmail_sender, "configured", lambda: False)
    assert client.post("/api/load-planning/email/confirmed/send", json=body()).status_code == 503


def test_gmail_failure_surfaces_status(client, monkeypatch):
    monkeypatch.setattr(gmail_sender, "configured", lambda: True)

    def boom(**kwargs):
        raise gmail_sender.GmailSendError("Gmail rejected", 502)

    monkeypatch.setattr(gmail_sender, "send_email", boom)
    assert client.post("/api/load-planning/email/confirmed/send", json=body()).status_code == 502
