"""Assignment emails: automatic on assign, one per driver, real status, no false success.

Mocked only: no Gmail, no Zoho, no database.
"""
from __future__ import annotations

import email as email_lib
import os
from contextlib import contextmanager
from datetime import date
from email import policy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost")

from auth.dependencies import CurrentUser, get_current_user  # noqa: E402
from routers import admin, assignment, gmail  # noqa: E402
from services import assignment_email_status, gmail_sender, live_sales_order_cache as lc  # noqa: E402

TEAM = [{"email": "ops@x.com"}, {"email": "boss@x.com"}]
STAFF = {
    1: {"id": 1, "name": "Juan", "email": "juan@x.com", "phone": "09171234567", "warehouse": "METS"},
    2: {"id": 2, "name": "Pedro", "email": None, "phone": "09170000002", "warehouse": "METS"},
    3: {"id": 3, "name": "Maria", "email": "maria@x.com", "phone": "09170000003", "warehouse": "GLACIER"},
}


class InlinePool:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def make_order(number):
    return SimpleNamespace(
        id=f"id-{number}", salesorder_number=number, customer_name=f"Customer {number}", shipping_address=None,
        raw_json={"shipping_address": {"address": f"{number} Road", "city": "Manila", "country": "Philippines"}, "line_items": [{"quantity": 10, "unit": "kg"}, {"quantity": 5, "unit": "kg"}]},
        assignment_status="unassigned", expected_shipment_date=date(2026, 10, 10), vehicle_id=None, driver_id=None, helper_ids=[], assigned_at=None, assigned_by=None,
    )


class Harness:
    def __init__(self, monkeypatch):
        self.sent: list[dict] = []
        self.fail_with: Exception | None = None
        self.orders: dict = {}
        self.notifications: list = []
        self.profile = SimpleNamespace(plate_no="TRK1", rated_capacity_kg=None, is_reefer=None, is_third_party=False, capacity_note=None)
        self.user = CurrentUser(id=1, email="pau@example.com", role="dispatcher", full_name="Pau", status="active")
        self.db = SimpleNamespace(execute=lambda stmt: SimpleNamespace(scalar_one_or_none=lambda: self.profile), commit=lambda: None)
        monkeypatch.delenv("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET", raising=False)
        monkeypatch.setattr(assignment, "_notification_pool", InlinePool())
        monkeypatch.setattr(assignment.gmail_sender, "configured", lambda: True)
        monkeypatch.setattr(assignment.gmail_sender, "send_email", self.fake_send)
        monkeypatch.setattr(assignment.staff_directory_cache, "get_by_id", lambda i: STAFF.get(i))
        monkeypatch.setattr(assignment.staff_directory_cache, "notify_list", lambda: TEAM)
        monkeypatch.setattr(assignment.live_sales_order_cache, "find_cached", lambda oid: self.orders.get(oid))
        monkeypatch.setattr(assignment.live_sales_order_cache, "get_assigned_snapshot", lambda: [])
        monkeypatch.setattr(assignment, "_weight_if_known", lambda o: 15.0)
        monkeypatch.setattr(assignment, "sync_history_row", lambda db, o: None)
        monkeypatch.setattr(assignment, "invalidate_fleet_cache", lambda: None)
        monkeypatch.setattr(assignment.vapi_client, "voice_provider", lambda: "n8n")
        monkeypatch.setattr(assignment, "_send_notification", lambda url, secret, payload: self.notifications.append(url))
        for control in (assignment.whatsapp_control, assignment.voice_control):
            monkeypatch.setattr(control, "is_active", lambda: True)
        self.ids: list[str] = []

    def fake_send(self, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(kwargs)
        return {"id": f"msg-{len(self.sent)}", "threadId": "t", "to": kwargs["to"]}

    def assign(self, numbers, driver_ids):
        orders = [make_order(n) for n in numbers]
        self.orders = {o.id: o for o in orders}
        self.ids = [o.id for o in orders]
        body = assignment.AssignmentBody(salesorder_ids=self.ids, vehicle_id="TRK1", driver_ids=driver_ids)
        return assignment.assign_order(self.ids[0], body, self.user, self.db)

    def status(self):
        return assignment_email_status.summarize(self.ids)

    def by_purpose(self, purpose):
        return [m for m in self.sent if m.get("purpose") == purpose]


@pytest.fixture
def h(monkeypatch):
    harness = Harness(monkeypatch)
    yield harness
    with lc._state_lock:
        for oid in harness.ids:
            lc._assignment_state.pop(oid, None)


def test_assign_sends_driver_and_team_email_automatically(h):
    result = h.assign(["SO-1"], [1])
    assert result["success"] is True
    driver, team = h.by_purpose("assignment-driver"), h.by_purpose("assignment-team")
    assert [m["to"] for m in driver] == ["juan@x.com"]
    assert driver[0]["subject"] == "Driver assignment — TRK1 — 1 sales order(s)"
    assert team[0]["to"] == ["ops@x.com", "boss@x.com"]
    assert h.status()["status"] == "sent" and h.status()["messageId"] == "msg-1"
    assert result["notifications"]["emailStatus"] == "queued"  # the HTTP answer is "queued"; the real outcome is the status above


def test_bulk_assign_sends_one_email_per_driver_with_all_their_sos(h):
    h.assign(["SO-1", "SO-2", "SO-3"], [1, 3])
    driver, team = h.by_purpose("assignment-driver"), h.by_purpose("assignment-team")
    assert sorted(m["to"] for m in driver) == ["juan@x.com", "maria@x.com"]  # 2 drivers -> 2 emails, not 6
    for message in driver:
        assert all(number in message["html"] for number in ("SO-1", "SO-2", "SO-3"))
    assert len(team) == 1 and all(number in team[0]["html"] for number in ("SO-1", "SO-2", "SO-3"))  # one team email for the batch
    assert h.status()["status"] == "sent"


def test_token_failure_records_failed_not_sent(h):
    h.fail_with = gmail_sender.GmailSendError("Gmail authorization failed (invalid_grant: Token has been expired or revoked). The mailbox may need to be reconnected.", 502)
    result = h.assign(["SO-1"], [1])
    assert result["success"] is True  # the assignment itself still stands
    status = h.status()
    assert status["status"] == "failed" and "invalid_grant" in status["error"]


def test_gmail_not_configured_is_a_visible_failure(h, monkeypatch):
    monkeypatch.setattr(assignment.gmail_sender, "configured", lambda: False)
    h.assign(["SO-1"], [1])
    assert h.status()["status"] == "failed" and "not configured" in h.status()["error"]
    assert h.sent == []


def test_driver_without_email_is_skipped_visibly_but_team_still_gets_the_email(h):
    h.assign(["SO-1"], [2])
    assert h.by_purpose("assignment-driver") == []
    assert len(h.by_purpose("assignment-team")) == 1
    status = h.status()
    assert status["status"] == "sent" and "driver Pedro: skipped: no email on file" in status["error"]


def test_no_driver_email_and_no_team_recipients_is_skipped(h, monkeypatch):
    monkeypatch.setattr(assignment.staff_directory_cache, "notify_list", lambda: [])
    h.assign(["SO-1"], [2])
    status = h.status()
    assert status["status"] == "skipped" and "no email on file" in status["error"]


def test_pause_flags_never_block_email(h, monkeypatch):
    monkeypatch.setenv("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET", "s")
    for control in (assignment.whatsapp_control, assignment.voice_control):
        monkeypatch.setattr(control, "is_active", lambda: False)  # WhatsApp and AI voice both paused
    monkeypatch.setattr(assignment.whatsapp_control, "log_skipped", lambda **k: None)
    monkeypatch.setattr(assignment.voice_control, "log_skipped", lambda **k: None)
    result = h.assign(["SO-1"], [1])
    assert len(h.by_purpose("assignment-driver")) == 1 and len(h.by_purpose("assignment-team")) == 1
    assert h.status()["status"] == "sent"
    assert result["notifications"]["channels"]["whatsapp"] == "paused" and result["notifications"]["channels"]["voice"] == "paused"
    assert not any("whatsapp" in url for url in h.notifications)  # WhatsApp really was skipped; email was not


def test_resend_is_explicit_email_only_and_bypasses_dedupe(h, monkeypatch):
    monkeypatch.setenv("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET", "s")
    h.assign(["SO-1"], [1])
    auto_key = h.by_purpose("assignment-driver")[0]["dedupe_key"]
    h.sent.clear()
    h.notifications.clear()
    body = assignment.AssignmentEmailBody(salesorder_ids=h.ids, vehicle_id="TRK1", driver_ids=[1], resend=True, html_body="<p>edited</p>", subject="Edited subject")
    assignment.send_assignment_email(body, h.user, h.db)
    assert auto_key.startswith("driver|juan@x.com|TRK1|")  # the automatic send is deduped on driver + vehicle + SO set
    assert [m["dedupe_key"] for m in h.sent] == [None]  # the explicit Resend is never swallowed by the 120s dedupe
    assert h.by_purpose("assignment-team") == [] and h.notifications == []  # no second team email, no second WhatsApp/SMS/voice
    assert h.sent[0]["subject"] == "Edited subject" and h.sent[0]["html"] == "<p>edited</p>"


def test_status_is_persisted_only_on_state_change(monkeypatch):
    writes = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, stmt):
            writes.append(stmt.compile().params)

        def commit(self):
            pass

    monkeypatch.setattr(assignment_email_status, "_open_session", lambda: Session())
    assignment_email_status.record(["a", "b"], "queued")
    assignment_email_status.record(["a", "b"], "sent", message_id="m1")
    assert len(writes) == 2  # queued -> sent: two writes for the whole batch, none while idle
    assert writes[1]["email_status"] == "sent" and writes[1]["email_message_id"] == "m1" and writes[1]["email_sent_at"] is not None
    assert writes[0]["email_sent_at"] is None


def test_status_endpoint_reads_memory_only(h):
    h.assign(["SO-1", "SO-2"], [1])
    app = FastAPI()
    app.include_router(assignment.router)
    app.dependency_overrides[get_current_user] = lambda: h.user
    body = TestClient(app).get("/api/load-planning/assignments/email-status", params={"ids": ",".join(h.ids)}).json()
    assert body["status"] == "sent" and body["messageId"] == "msg-1"


# ---- From header and message format ------------------------------------------------------------------

def raw_to_message(raw):
    import base64
    return email_lib.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default)


@pytest.mark.parametrize("name,address", [
    ("Martin Cuico", "martin.logistics@rareglobalfood.com"),
    ("Martin Cuico", "Martin Cuico <martin.logistics@rareglobalfood.com>"),          # whole identity pasted into the address variable
    ("Martin Cuico <martin.logistics@rareglobalfood.com>", "martin.logistics@rareglobalfood.com"),  # ... or into the name variable
    ('"Martin Cuico" <martin.logistics@rareglobalfood.com>', "<martin.logistics@rareglobalfood.com>"),
])
def test_from_header_is_exactly_name_and_address(name, address):
    raw = gmail_sender.build_raw_message(to=["a@x.com"], subject="s", html="<p>x</p>", sender=address, from_name=name)
    assert raw_to_message(raw)["From"] == "Martin Cuico <martin.logistics@rareglobalfood.com>"


def test_assignment_email_has_five_column_table_inline_css_and_plain_text_part():
    drivers = [SimpleNamespace(name="Juan", warehouse="METS", email="juan@x.com")]
    profile = SimpleNamespace(plate_no="Motorcycle 1", capacity_note=None)
    orders = [make_order("SO-1"), make_order("SO-2")]
    subject, html, _ = assignment._assignment_email_html(drivers, profile, orders, "Pau", "2026-10-09T00:00:00+00:00", weight_fn=lambda o: 15.0)
    text = assignment._assignment_email_text(profile, orders, weight_fn=lambda o: 15.0)
    assert subject == "Driver assignment — Motorcycle 1 — 2 sales order(s)"
    assert "Route details for the assigned driver." in html and "reply to confirm receipt" in html
    for column in ("SO number", "Client", "Total kg", "Total Packs", "Shipping Address"):
        assert f">{column}</th>" in html
    table = html[html.index("<table cellspacing='0' cellpadding='0' border='0' style='border-collapse:collapse"):]
    assert "<style" not in table and "background:#eef1f4" in table and "border:1px solid" in table  # inline CSS: shaded header, bordered cells
    assert "overflow-x:auto" in html  # scrolls inside its wrapper on a phone
    raw = gmail_sender.build_raw_message(to=["juan@x.com"], subject=subject, html=html, text=text, sender="martin.logistics@rareglobalfood.com", from_name="Martin Cuico")
    message = raw_to_message(raw)
    plain, rich = message.get_body(("plain",)), message.get_body(("html",))
    assert plain is not None and rich is not None
    assert "SO number: SO-1" in plain.get_content() and "Shipping Address: SO-1 Road" in plain.get_content()


# ---- Gmail auth state (banner) -------------------------------------------------------------------------

def fake_google(status, body):
    return lambda *a, **k: SimpleNamespace(status_code=status, json=lambda: body, text=str(body))


@pytest.fixture
def gmail_env(monkeypatch):
    for key in ("GMAIL_COMMS_CLIENT_ID", "GMAIL_COMMS_CLIENT_SECRET", "GMAIL_COMMS_REFRESH_TOKEN"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setattr(gmail_sender, "_auth", {"ok": None, "error": None, "checked_at": 0.0})
    monkeypatch.setattr(gmail_sender, "_token", {"value": None, "expires_at": 0.0})


def test_revoked_token_drives_the_disconnected_banner_endpoint(gmail_env, monkeypatch):
    monkeypatch.setattr(gmail_sender.httpx, "post", fake_google(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}))
    app = FastAPI()
    app.include_router(gmail.router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=1, email="a@x.com", role="dispatcher", full_name="A", status="active")
    body = TestClient(app).get("/api/gmail/auth-status").json()
    assert body["connected"] is False and "invalid_grant" in body["error"]


def test_good_token_is_connected_and_check_is_throttled(gmail_env, monkeypatch):
    calls = []
    monkeypatch.setattr(gmail_sender.httpx, "post", lambda *a, **k: (calls.append(1), SimpleNamespace(status_code=200, json=lambda: {"access_token": "t", "expires_in": 3600}))[1])
    assert gmail_sender.auth_status()["connected"] is True
    assert gmail_sender.auth_status()["connected"] is True
    assert len(calls) == 1  # re-checked at most every 5 minutes


def test_network_blip_is_not_reported_as_disconnected(gmail_env, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(gmail_sender.httpx, "post", boom)
    assert gmail_sender.auth_status()["connected"] is True


def test_not_configured_is_disconnected(monkeypatch):
    monkeypatch.delenv("GMAIL_COMMS_REFRESH_TOKEN", raising=False)
    assert gmail_sender.auth_status()["connected"] is False


# ---- admin test endpoint ---------------------------------------------------------------------------------

def admin_client(role="admin"):
    app = FastAPI()
    app.include_router(admin.usage_router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=1, email="a@x.com", role=role, full_name="A", status="active")
    return TestClient(app)


def test_test_endpoint_sends_marked_email_through_the_real_path_without_zoho(monkeypatch):
    sent = []
    monkeypatch.setattr(gmail_sender, "send_email", lambda **k: (sent.append(k), {"id": "m9"})[1])
    monkeypatch.setattr(gmail_sender, "recent", lambda n=1: [{"sender": "martin.logistics@rareglobalfood.com"}])
    monkeypatch.setenv("GMAIL_FROM_NAME", "Martin Cuico")

    def no_zoho(*a, **k):
        raise AssertionError("the test endpoint must never call Zoho")

    from services import zoho_client
    monkeypatch.setattr(zoho_client.httpx, "request", no_zoho)
    response = admin_client().post("/api/admin/test-assignment-email", json={"driver_email": "me@x.com"})
    assert response.status_code == 200
    body = response.json()
    assert body["subject"].startswith("[TEST] Driver assignment — TEST-TRUCK — 2 sales order(s)")
    assert body["from"] == "Martin Cuico <martin.logistics@rareglobalfood.com>" and body["source"] == "sample rows"
    assert sent[0]["to"] == "me@x.com" and sent[0]["label_logistics"] is False and sent[0]["purpose"] == "assignment-test"
    assert "SO number: SO-TEST-0001" in sent[0]["text"] and ">Shipping Address</th>" in sent[0]["html"]


def test_test_endpoint_is_admin_only_and_reports_real_failures(monkeypatch):
    assert admin_client("dispatcher").post("/api/admin/test-assignment-email", json={"driver_email": "me@x.com"}).status_code == 403

    def fail(**k):
        raise gmail_sender.GmailSendError("Gmail authorization failed (invalid_grant)", 502)
    monkeypatch.setattr(gmail_sender, "send_email", fail)
    response = admin_client().post("/api/admin/test-assignment-email", json={"driver_email": "me@x.com"})
    assert response.status_code == 502 and "invalid_grant" in response.json()["detail"]


# ---- persisted send log ---------------------------------------------------------------------------------

def test_send_log_reads_persisted_status_so_it_survives_restarts(monkeypatch):
    rows = [SimpleNamespace(id="id-1", salesorder_number="SO-1", vehicle_id="TRK1", assigned_at=None, email_status="failed", email_error="invalid_grant", email_sent_at=None, email_message_id=None),
            SimpleNamespace(id="id-2", salesorder_number="SO-2", vehicle_id="TRK1", assigned_at=None, email_status="failed", email_error="invalid_grant", email_sent_at=None, email_message_id=None)]

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    import database
    monkeypatch.setattr(database, "SessionLocal", lambda: Session())
    result = gmail._persisted_assignment_emails(50)
    assert result == [{"vehicle": "TRK1", "assignedAt": None, "status": "failed", "error": "invalid_grant", "sentAt": None, "messageId": None, "salesOrders": ["SO-1", "SO-2"]}]


# ---- assignment_batch_id: Retry / Resend can never merge two assignments ---------------------------------------

def test_two_assignments_to_the_same_truck_get_their_own_batch_and_never_merge(h):
    first = h.assign(["SO-A"], [1])
    ids_a, orders_a = list(h.ids), dict(h.orders)
    second = h.assign(["SO-B"], [1])
    ids_b = list(h.ids)
    h.orders.update(orders_a)
    try:
        batch_a, batch_b = first["assignment_batch_id"], second["assignment_batch_id"]
        assert batch_a and batch_b and batch_a != batch_b
        assert lc.ids_for_batch(batch_a) == ids_a and lc.ids_for_batch(batch_b) == ids_b
        h.sent.clear()
        # Retry of assignment A: resolved from its batch id, so the email contains SO-A only - never SO-B.
        body = assignment.AssignmentEmailBody(salesorder_ids=ids_a, vehicle_id="TRK1", driver_ids=[1], email_only=True, assignment_batch_id=batch_a)
        assignment.send_assignment_email(body, h.user, h.db)
        driver_mail = h.by_purpose("assignment-driver")
        assert len(driver_mail) == 1 and "SO-A" in driver_mail[0]["html"] and "SO-B" not in driver_mail[0]["html"]
        assert lc.ids_for_batch(batch_a) == ids_a  # a retry never rewrites the batch id
        # Listing orders of both assignments under one batch id is refused, not merged.
        mixed = assignment.AssignmentEmailBody(salesorder_ids=ids_a + ids_b, vehicle_id="TRK1", driver_ids=[1], resend=True, assignment_batch_id=batch_a)
        with pytest.raises(assignment.HTTPException) as refused:
            assignment.send_assignment_email(mixed, h.user, h.db)
        assert refused.value.status_code == 409
        unknown = assignment.AssignmentEmailBody(salesorder_ids=ids_a, vehicle_id="TRK1", driver_ids=[1], resend=True, assignment_batch_id="nope")
        with pytest.raises(assignment.HTTPException) as missing:
            assignment.send_assignment_email(unknown, h.user, h.db)
        assert missing.value.status_code == 404
    finally:
        with lc._state_lock:
            for oid in ids_a:
                lc._assignment_state.pop(oid, None)


def test_retry_resolves_the_whole_assignment_even_if_only_one_order_is_listed(h):
    result = h.assign(["SO-1", "SO-2", "SO-3"], [1])
    h.sent.clear()
    body = assignment.AssignmentEmailBody(salesorder_ids=h.ids[:1], vehicle_id="TRK1", driver_ids=[1], email_only=True, assignment_batch_id=result["assignment_batch_id"])
    assignment.send_assignment_email(body, h.user, h.db)
    html = h.by_purpose("assignment-driver")[0]["html"]
    assert all(number in html for number in ("SO-1", "SO-2", "SO-3"))


def test_batch_id_is_written_in_the_same_update_as_the_status_with_no_extra_write(h, monkeypatch):
    writes = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, stmt):
            writes.append(stmt.compile().params)

        def commit(self):
            pass

    monkeypatch.setattr(assignment_email_status, "_open_session", lambda: Session())
    result = h.assign(["SO-1", "SO-2"], [1])
    assert len(writes) == 2  # queued, then sent: the batch id rides in the first one
    assert writes[0]["email_status"] == "queued" and writes[0]["assignment_batch_id"] == result["assignment_batch_id"]
    assert writes[1]["email_status"] == "sent" and "assignment_batch_id" not in writes[1]
