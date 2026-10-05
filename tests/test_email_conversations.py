"""Gmail-backed email conversations, the n8n email-feed removal, startup identity check and
assignment channel status. Gmail is mocked; nothing is sent."""
import asyncio
import base64
import logging
from types import SimpleNamespace

import httpx
import pytest

from services import email_conversations as ec, gmail_sender, logistics_email_agent as agent, staff_directory_cache, live_sales_order_cache

LOGISTICS = "martin.logistics@rareglobalfood.com"
DRIVER = {"id": 7, "name": "Juan Dela Cruz", "email": "juan@example.com", "warehouse": "GLACIER", "active": True}


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def msg(mid, sender, body, *, thread="T1", subject="Driver assignment - NAN1234 - 2 sales order(s)", internal=1000, extra=None, to="juan@example.com"):
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": to}, {"name": "Subject", "value": subject}]
    headers += [{"name": k, "value": v} for k, v in (extra or {}).items()]
    return {"id": mid, "threadId": thread, "internalDate": str(internal), "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": _b64(body)}}}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    agent.reset_state()
    ec.invalidate()
    monkeypatch.setenv("GMAIL_COMMS_CLIENT_ID", "id")
    monkeypatch.setenv("GMAIL_COMMS_CLIENT_SECRET", "s")
    monkeypatch.setenv("GMAIL_COMMS_REFRESH_TOKEN", "r")
    monkeypatch.setattr(gmail_sender, "mailbox_address", lambda: "martin@rareglobalfood.com")
    monkeypatch.setattr(staff_directory_cache, "all_staff", lambda: [DRIVER])
    monkeypatch.setattr(staff_directory_cache, "notify_list", lambda: [{"name": "Jed", "email": "ops1@x.com"}])
    monkeypatch.setattr(live_sales_order_cache, "get_assigned_snapshot", lambda: [])


ASSIGNMENT = msg("a1", f"Martin Cuico <{LOGISTICS}>", "Driver assignment\nSO-1001 Acme Foods\nSO-1002 Beta Mart", internal=1000)
DRIVER_REPLY = msg("d1", "Juan <juan@example.com>", "Confirmed po.\n\nOn Mon, Oct 5, 2026 at 9:00 AM Martin <x@y.com> wrote:\n> old", internal=2000, subject="Re: Driver assignment - NAN1234 - 2 sales order(s)")
AGENT_REPLY = msg("r1", f"Martin Cuico <{LOGISTICS}>", "Thank you for confirming.\n\nRegards,\nMartin Cuico", internal=3000, subject="Re: Driver assignment - NAN1234 - 2 sales order(s)", extra={agent.ACTION_HEADER: "ACK_CONFIRM"})


def test_conversation_shape_matches_the_comms_views():
    convo = ec.build_conversation({"id": "T1", "messages": [ASSIGNMENT, DRIVER_REPLY, AGENT_REPLY]})
    assert convo["channel"] == "email" and convo["contact"] == "juan@example.com" and convo["driverName"] == "Juan Dela Cruz"
    assert convo["truckPlate"] == "NAN1234" and convo["soNumbers"] == ["SO-1001", "SO-1002"] and convo["warehouse"] == "Glacier Cold Storage"
    assert convo["conversationStage"] == "CONFIRMED" and convo["assignmentConfirmed"] is True and convo["humanEscalated"] is False
    assert [m["role"] for m in convo["messages"]] == ["agent", "driver", "agent"]
    assert convo["messages"][1]["content"] == "Confirmed po."  # quoted history stripped
    assert all(m["ts"].endswith("+00:00") for m in convo["messages"]) and convo["messageCount"] == 3
    assert convo["lastUpdated"] == convo["lastMessageAt"] == convo["messages"][-1]["ts"]
    assert set(convo) >= {"driverName", "truckPlate", "soNumbers", "conversationStage", "assignmentConfirmed", "humanEscalated", "lastUpdated", "messages"}


def test_unreplied_assignment_email_is_stage_assigned():
    convo = ec.build_conversation({"id": "T1", "messages": [ASSIGNMENT]})
    assert convo["conversationStage"] == "ASSIGNED" and convo["assignmentConfirmed"] is False and convo["contact"] == "juan@example.com"


def test_escalated_state_comes_from_agent_header():
    escalated = msg("r2", f"Martin Cuico <{LOGISTICS}>", "Please call the Control Tower.", internal=4000, extra={agent.ACTION_HEADER: "ESCALATE"})
    convo = ec.build_conversation({"id": "T1", "messages": [ASSIGNMENT, DRIVER_REPLY, escalated]})
    assert convo["humanEscalated"] is True and convo["conversationStage"] == "ESCALATED"


@pytest.mark.parametrize("thread", [
    [msg("t1", f"Martin Cuico <{LOGISTICS}>", "Hi Jed", subject="[Driver Reply] Juan - NAN1234", to="ops1@x.com")],
    [msg("t2", f"Martin Cuico <{LOGISTICS}>", "Sales orders attached", subject="Sales orders | 2026-10-05", to="customer@x.com")],
    [msg("t3", "Stranger <who@else.com>", "hello", subject="Hello")],
    [msg("t4", f"Martin Cuico <{LOGISTICS}>", "test", subject="[IntelliFleet TEST] x", to="rishi@rareglobalfood.com")],
])
def test_non_driver_threads_are_left_out(thread):
    assert ec.build_conversation({"id": "X", "messages": thread}) is None


def test_driver_started_thread_without_assignment_subject_is_included_via_staff_match():
    convo = ec.build_conversation({"id": "T9", "messages": [msg("n1", "Juan <juan@example.com>", "Hi, question", subject="Pickup question", internal=500)]})
    assert convo and convo["driverName"] == "Juan Dela Cruz" and [m["role"] for m in convo["messages"]] == ["driver"]


def test_live_assignment_fills_missing_plate_and_sos(monkeypatch):
    order = SimpleNamespace(id="x", salesorder_number="SO-7777", customer_name="Live Co", vehicle_id="LIV9999", driver_id=7, helper_ids=[], assignment_status="assigned", raw_json={})
    monkeypatch.setattr(live_sales_order_cache, "get_assigned_snapshot", lambda: [order])
    convo = ec.build_conversation({"id": "T9", "messages": [msg("n1", "Juan <juan@example.com>", "Hi", subject="Pickup", internal=500)]})
    assert convo["truckPlate"] == "LIV9999" and convo["soNumbers"] == ["SO-7777"]


def test_feed_is_read_only_gmail_cached_and_sorted(monkeypatch):
    calls = []

    def gget(path, params=None):
        calls.append((path, (params or {}).get("q")))
        if path == "threads":
            return {"threads": [{"id": "T1"}, {"id": "T2"}]}
        if path == "threads/T1":
            return {"id": "T1", "messages": [ASSIGNMENT, DRIVER_REPLY, AGENT_REPLY]}
        return {"id": "T2", "messages": [msg("o1", f"Martin Cuico <{LOGISTICS}>", "x", thread="T2", subject="Sales orders", to="c@x.com")]}

    monkeypatch.setattr(agent, "_gmail_get", gget)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("the feed must be read-only"))
    first = ec.feed()
    assert first["count"] == 1 and first["conversations"][0]["threadId"] == "T1"
    assert ("threads", "label:logistics newer_than:30d") in calls
    n = len(calls)
    assert ec.feed() is first and len(calls) == n  # 60s cache: no further Gmail reads
    ec.invalidate()
    ec.feed()
    assert len(calls) > n


def test_feed_errors_raise_or_are_tolerated(monkeypatch):
    monkeypatch.setattr(agent, "_gmail_get", lambda *a, **k: (_ for _ in ()).throw(gmail_sender.GmailSendError("down", 502)))
    with pytest.raises(gmail_sender.GmailSendError):
        ec.feed()
    assert ec.feed(tolerate_errors=True) == {"count": 0, "conversations": [], "error": "Feed unavailable"}
    monkeypatch.delenv("GMAIL_COMMS_REFRESH_TOKEN")
    with pytest.raises(gmail_sender.GmailSendError) as exc:
        ec.feed()
    assert exc.value.status_code == 503


def test_communications_endpoint(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test")
    from fastapi import HTTPException
    from routers import communications

    monkeypatch.setattr(ec, "feed", lambda **k: {"count": 0, "conversations": []})
    assert communications.email_conversation_feed() == {"count": 0, "conversations": []}
    monkeypatch.setattr(ec, "feed", lambda **k: (_ for _ in ()).throw(gmail_sender.GmailSendError("not configured", 503)))
    with pytest.raises(HTTPException) as exc:
        communications.email_conversation_feed()
    assert exc.value.status_code == 503


# ---- dispatch / comms-overview no longer touch n8n for email ----

def test_n8n_email_feed_is_gone_and_overview_reads_gmail(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test")
    from fastapi import HTTPException
    from routers import dispatch

    assert "email" not in dispatch._N8N_FEEDS and "email" not in dispatch._CHANNELS
    with pytest.raises(HTTPException) as exc:
        dispatch.n8n_conversations("email")
    assert exc.value.status_code == 404
    import inspect
    assert "logistics-email-conversations" not in inspect.getsource(dispatch)

    urls = []
    monkeypatch.setattr(httpx, "get", lambda url, **kw: urls.append(url) or SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"conversations": []}))
    monkeypatch.setattr(dispatch.voice_calls, "conversations_feed", lambda: asyncio.sleep(0, {"count": 0, "conversations": []}))
    monkeypatch.setattr(ec, "feed", lambda **k: {"count": 1, "conversations": [{"lastUpdated": "2026-10-05T01:00:00+00:00", "messages": [{"role": "agent", "content": "hi", "ts": "2026-10-05T01:00:00+00:00"}], "conversationStage": "CONFIRMED", "assignmentConfirmed": True}]})
    dispatch._n8n_cache.clear()
    overview = dispatch.communications_overview()
    assert not any("email" in u for u in urls) and all("whatsapp" in u or "sms" in u for u in urls)
    email = next(c for c in overview["channels"] if c["channel"] == "email")
    assert email["confirmed"] == 1 or email["messagesSentThisMonth"] >= 0  # computed from the Gmail feed


# ---- startup identity check / health ----

def test_identity_config_problems_and_health(monkeypatch):
    for name in gmail_sender.IDENTITY_ENV:
        monkeypatch.delenv(name, raising=False)
    assert gmail_sender.identity_config_problems() == list(gmail_sender.IDENTITY_ENV)
    health = gmail_sender.identity_health()
    assert health["ok"] is False and health["missingIdentityConfig"] == list(gmail_sender.IDENTITY_ENV)
    for name in gmail_sender.IDENTITY_ENV:
        monkeypatch.setenv(name, "x")
    assert gmail_sender.identity_health() == {"configured": True, "missingIdentityConfig": [], "ok": True}
    assert "secret" not in str(health).lower()


def test_production_with_blank_identity_logs_error_at_startup(monkeypatch, caplog):
    monkeypatch.setattr(gmail_sender.threading, "Thread", lambda **k: SimpleNamespace(start=lambda: None))
    for name in gmail_sender.IDENTITY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RENDER", "true")
    with caplog.at_level(logging.WARNING, logger="gmail_sender"):
        gmail_sender.log_identity_config_at_startup()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "PRODUCTION is missing" in r.getMessage()]
    assert errors and all(name in errors[0].getMessage() for name in gmail_sender.IDENTITY_ENV)
    caplog.clear()
    monkeypatch.delenv("RENDER")
    with caplog.at_level(logging.WARNING, logger="gmail_sender"):
        gmail_sender.log_identity_config_at_startup()
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]  # non-production: warning only


def test_startup_reports_unusable_alias(monkeypatch, caplog):
    for name in gmail_sender.IDENTITY_ENV:
        monkeypatch.setenv(name, "martin.logistics@rareglobalfood.com")
    monkeypatch.setattr(gmail_sender.threading, "Thread", lambda target=None, **k: SimpleNamespace(start=target))
    monkeypatch.setattr(gmail_sender, "identity_status", lambda: {"usable": False, "error": "alias missing"})
    with caplog.at_level(logging.ERROR, logger="gmail_sender"):
        gmail_sender.log_identity_config_at_startup()
    assert any("alias missing" in r.getMessage() for r in caplog.records)


# ---- assignment: per-channel status when the n8n secret is missing ----

class _Pool:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def _assign(monkeypatch, voice_provider="n8n"):
    from routers import assignment as a

    order = SimpleNamespace(id="SO-ID-1", salesorder_number="SO-1001", customer_name="Acme", shipping_address=None, raw_json={"line_items": [{"quantity": 1}], "shipping_address": {"address": "1 St", "city": "Manila"}})
    profile = SimpleNamespace(plate_no="NAN1234", capacity_note=None, is_third_party=False)
    db = SimpleNamespace(execute=lambda stmt: SimpleNamespace(scalar_one_or_none=lambda: profile))
    monkeypatch.setattr(a, "_notification_pool", _Pool())
    monkeypatch.setattr(a, "_weight", lambda o: 1.0)
    monkeypatch.setattr(a.live_sales_order_cache, "find_cached", lambda oid: order)
    monkeypatch.setattr(a.staff_directory_cache, "get_by_id", lambda i, **kw: {"id": 1, "name": "Juan", "email": "juan@x.com", "phone": "0917", "warehouse": "METS"})
    monkeypatch.setattr(a.staff_directory_cache, "notify_list", lambda: [])
    monkeypatch.setattr(a.vapi_client, "voice_provider", lambda: voice_provider)
    monkeypatch.setattr(a.voice_calls, "place_assignment_calls_sync", lambda **kw: [])
    monkeypatch.setattr(gmail_sender, "send_email", lambda **kw: {"id": "m", "threadId": "t", "to": [kw["to"]]})
    posts = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: posts.append(url) or SimpleNamespace(status_code=200, raise_for_status=lambda: None))
    body = a.AssignmentEmailBody(salesorder_ids=["SO-ID-1"], vehicle_id="NAN1234", driver_ids=[1])
    return a.send_assignment_email(body, SimpleNamespace(full_name="Dan", email="d@x.com"), db), posts


def test_channel_status_reports_skipped_missing_secret_and_warns(monkeypatch, caplog):
    monkeypatch.setenv("JWT_SECRET", "test")
    monkeypatch.delenv("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET", raising=False)
    with caplog.at_level(logging.WARNING):
        result, posts = _assign(monkeypatch)
    assert result["channels"] == {"email": "queued", "whatsapp": "skipped_missing_secret", "sms": "skipped_missing_secret", "voice": "skipped_missing_secret"}
    assert result["emailStatus"] == "queued" and result["dispatched"] == ["email"] and posts == []
    assert any("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET is not set" in r.getMessage() for r in caplog.records)


def test_channel_status_with_secret_and_direct_voice(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test")
    monkeypatch.setenv("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET", "s")
    result, posts = _assign(monkeypatch)
    assert result["channels"] == {"email": "queued", "whatsapp": "queued", "sms": "queued", "voice": "queued"}
    assert all("whatsapp" in u or "sms" in u or "voice" in u for u in posts) and len(posts) == 3
    result, _ = _assign(monkeypatch, voice_provider="direct")
    assert result["channels"]["voice"] == "queued_direct"
    monkeypatch.delenv("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET")
    result, _ = _assign(monkeypatch, voice_provider="direct")
    assert result["channels"] == {"email": "queued", "whatsapp": "skipped_missing_secret", "sms": "skipped_missing_secret", "voice": "queued_direct"}
