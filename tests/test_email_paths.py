"""Email paths at the Gmail service boundary: no real Gmail call, and any n8n call fails the test."""
import base64
import email
from email import policy
from types import SimpleNamespace

import httpx
import pytest

from services import gmail_sender


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test")
    monkeypatch.setenv("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET", "s")
    monkeypatch.setenv("GMAIL_COMMS_CLIENT_ID", "id")
    monkeypatch.setenv("GMAIL_COMMS_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GMAIL_COMMS_REFRESH_TOKEN", "refresh")
    gmail_sender._sent_keys.clear()


@pytest.fixture
def spy(monkeypatch):
    """Capture gmail_sender.send_email calls; record any other outbound HTTP POST (n8n)."""
    sends, posts = [], []

    def fake_send(**kwargs):
        sends.append(kwargs)
        return {"id": "m", "threadId": "t", "to": [kwargs["to"]] if isinstance(kwargs["to"], str) else kwargs["to"]}

    monkeypatch.setattr(gmail_sender, "send_email", fake_send)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: posts.append(url) or SimpleNamespace(status_code=200, raise_for_status=lambda: None))
    return SimpleNamespace(sends=sends, posts=posts)


# ---- Inventory / Load Planning / Confirmed SO: all share POST /api/load-planning/email/send ----

@pytest.mark.parametrize("assignment, layout", [(None, "default"), ("assigned", "confirmed")])
def test_email_send_inventory_load_planning_and_confirmed(monkeypatch, spy, assignment, layout):
    from routers import load_planning as lp

    layouts = []
    rows = [SimpleNamespace(id="1")]
    monkeypatch.setattr(lp, "_email_rows", lambda db, body: rows)
    monkeypatch.setattr(lp, "_confirmed_export_rows", lambda r: [["confirmed-line"]])
    monkeypatch.setattr(lp, "flatten_order", lambda order: [["plain-line"]])
    monkeypatch.setattr(lp, "make_pdf", lambda export_rows, caption, lay: layouts.append(lay) or SimpleNamespace(getvalue=lambda: b"%PDF-" + str(export_rows).encode()))
    monkeypatch.setattr(lp, "make_excel", lambda export_rows, caption, lay: SimpleNamespace(getvalue=lambda: b"XLSX-" + str(export_rows).encode()))

    body = lp.EmailSendRequest(to=" ops@example.com ", subject=" Sales Orders ", htmlBody="<p>Hello</p>", assignment=assignment, date_from="2026-10-05", date_to="2026-10-05", status="confirmed")
    result = lp.email_send(body, db=None)

    assert result["success"] and result["orderCount"] == 1
    assert len(spy.sends) == 1 and spy.posts == []
    sent = spy.sends[0]
    assert sent["to"] == "ops@example.com"
    assert sent["subject"] == "Sales Orders"
    assert sent["html"] == "<p>Hello</p>"
    assert sent["purpose"] == "sales-order-email"
    names = {name: (mime, data) for name, mime, data in sent["attachments"]}
    assert set(names) == {"SalesOrders.pdf", "SalesOrders.xlsx"}
    assert names["SalesOrders.pdf"][0] == "application/pdf" and names["SalesOrders.pdf"][1].startswith(b"%PDF-")
    assert names["SalesOrders.xlsx"][0].endswith("spreadsheetml.sheet")
    assert layouts == [layout]
    expected_line = "confirmed-line" if assignment == "assigned" else "plain-line"
    assert expected_line.encode() in names["SalesOrders.pdf"][1]


def test_email_send_rejects_bad_recipient_and_unconfigured(monkeypatch, spy):
    from fastapi import HTTPException
    from routers import load_planning as lp

    with pytest.raises(HTTPException) as bad:
        lp.email_send(lp.EmailSendRequest(to="nope", subject="s", htmlBody="x"), db=None)
    assert bad.value.status_code == 422
    monkeypatch.delenv("GMAIL_COMMS_REFRESH_TOKEN")
    with pytest.raises(HTTPException) as off:
        lp.email_send(lp.EmailSendRequest(to="a@x.com", subject="s", htmlBody="x"), db=None)
    assert off.value.status_code == 503
    assert spy.sends == [] and spy.posts == []


# ---- Driver + team confirmation, through the real send_assignment_email route function ----

class _Pool:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def _run_assignment(monkeypatch, spy, drivers):
    from routers import assignment as a

    order = SimpleNamespace(id="SO-ID-1", salesorder_number="SO-1001", customer_name="Acme Foods", shipping_address=None, raw_json={"line_items": [{"quantity": 4}], "shipping_address": {"address": "1 Cold St", "city": "Manila"}})
    profile = SimpleNamespace(plate_no="NAN1234", capacity_note=None, is_third_party=False)
    db = SimpleNamespace(execute=lambda stmt: SimpleNamespace(scalar_one_or_none=lambda: profile))
    staff = {d["id"]: d for d in drivers}
    monkeypatch.setattr(a, "_notification_pool", _Pool())
    monkeypatch.setattr(a, "_weight", lambda o: 120.0)
    monkeypatch.setattr(a.live_sales_order_cache, "find_cached", lambda oid: order)
    monkeypatch.setattr(a.staff_directory_cache, "get_by_id", lambda i, **kw: staff.get(i))
    monkeypatch.setattr(a.staff_directory_cache, "notify_list", lambda: [{"email": "ops1@x.com"}, {"email": "ops2@x.com"}, {"email": None}])
    monkeypatch.setattr(a.vapi_client, "voice_provider", lambda: "n8n")
    user = SimpleNamespace(full_name="Dispatcher Dan", email="d@x.com")
    body = a.AssignmentEmailBody(salesorder_ids=["SO-ID-1"], vehicle_id="NAN1234", driver_ids=[d["id"] for d in drivers])
    return a.send_assignment_email(body, user, db)


def test_driver_and_team_emails_use_gmail_and_never_n8n_email(monkeypatch, spy):
    drivers = [{"id": 1, "name": "Juan Dela Cruz", "email": "juan@x.com", "phone": "09171234567", "warehouse": "METS"}]
    result = _run_assignment(monkeypatch, spy, drivers)

    assert result["success"] and result["emailStatus"] == "queued"
    by_purpose = {s["purpose"]: s for s in spy.sends}
    driver, team = by_purpose["assignment-driver"], by_purpose["assignment-team"]
    assert driver["to"] == "juan@x.com"
    assert "NAN1234" in driver["subject"] and "1 sales order" in driver["subject"]
    for needle in ("SO-1001", "Acme Foods", "NAN1234"):
        assert needle in driver["html"]
    assert team["to"] == ["ops1@x.com", "ops2@x.com"]
    assert team["subject"] == "Assignment Confirmed: Juan Dela Cruz - NAN1234"
    assert "Juan Dela Cruz" in team["html"] and "NAN1234" in team["html"] and "Mets Cold Storage" in team["html"]
    assert len(spy.sends) == 2
    assert not any("driver-assignment" in url for url in spy.posts)  # the n8n email webhook
    assert all("whatsapp" in u or "sms" in u or "voice" in u for u in spy.posts)  # only non-email n8n channels remain


def test_multi_driver_sends_one_team_email_and_one_per_driver(monkeypatch, spy):
    drivers = [
        {"id": 1, "name": "Juan", "email": "juan@x.com", "phone": "0917", "warehouse": "METS"},
        {"id": 2, "name": "Pedro", "email": "pedro@x.com", "phone": "0918", "warehouse": "METS"},
    ]
    _run_assignment(monkeypatch, spy, drivers)
    assert sorted(s["to"] for s in spy.sends if s["purpose"] == "assignment-driver") == ["juan@x.com", "pedro@x.com"]
    assert sum(s["purpose"] == "assignment-team" for s in spy.sends) == 1


def test_gmail_not_configured_never_falls_back_to_n8n_email(monkeypatch, spy):
    monkeypatch.delenv("GMAIL_COMMS_REFRESH_TOKEN")
    result = _run_assignment(monkeypatch, spy, [{"id": 1, "name": "Juan", "email": "juan@x.com", "phone": "0917", "warehouse": "METS"}])
    assert result["success"] and result["emailStatus"] == "not_configured"
    assert spy.sends == []
    assert not any("driver-assignment" in url for url in spy.posts)


def test_assignment_survives_gmail_failure(monkeypatch, spy):
    """Gmail errors are swallowed in the background sender; the assignment call still succeeds."""
    def boom(**kwargs):
        raise gmail_sender.GmailSendError("Gmail down", 502)

    monkeypatch.setattr(gmail_sender, "send_email", boom)
    result = _run_assignment(monkeypatch, spy, [{"id": 1, "name": "Juan", "email": "juan@x.com", "phone": "0917", "warehouse": "METS"}])
    assert result["success"]


# ---- Dispatch email channel ----

def test_dispatch_email_channel_is_gmail_only(monkeypatch, spy):
    import asyncio

    from routers import dispatch as d

    monkeypatch.setenv("DISPATCH_N8N_DRIVER_BROADCAST_URL", "https://n8n.example/hook")
    body = d.SendMessageBody(audience="driver", recipient_name="Juan", recipient_email="juan@x.com", channels=["email"], subject="Hi", body="Line1\nLine2")
    result = asyncio.run(d.send_message(body))
    assert len(spy.sends) == 1 and spy.sends[0]["to"] == "juan@x.com" and spy.sends[0]["purpose"] == "dispatch-email"
    assert result["messages"][0]["status"] == "sent"
    assert spy.posts == []


def test_dispatch_email_failure_is_failed_not_n8n(monkeypatch, spy):
    import asyncio

    from routers import dispatch as d

    monkeypatch.setenv("DISPATCH_N8N_DRIVER_BROADCAST_URL", "https://n8n.example/hook")
    monkeypatch.setattr(gmail_sender, "send_email", lambda **kw: (_ for _ in ()).throw(gmail_sender.GmailSendError("down", 502)))
    body = d.SendMessageBody(audience="driver", recipient_name="Juan", recipient_email="juan@x.com", channels=["email"], subject="Hi", body="x")
    result = asyncio.run(d.send_message(body))
    assert result["messages"][0]["status"] == "failed" and spy.posts == []


def test_missing_n8n_secret_does_not_block_or_alter_email(monkeypatch, spy):
    monkeypatch.delenv("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET")
    result = _run_assignment(monkeypatch, spy, [{"id": 1, "name": "Juan", "email": "juan@x.com", "phone": "0917", "warehouse": "METS"}])
    assert result["success"] and {s["purpose"] for s in spy.sends} == {"assignment-driver", "assignment-team"}
    assert spy.posts == []  # no n8n channel is called without its secret
