"""Admin pause/resume switch for outbound AI voice calls. Vapi/Twilio/n8n are mocked; nothing is dialled."""
import asyncio
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test")

from auth.dependencies import CurrentUser, get_current_user
from routers import dispatch, voice
from services import gmail_sender, staff_directory_cache, vapi_client, voice_calls, voice_control
from tests.test_email_conversations import _assign

PHONE = "0917 123 4567"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.delenv("VOICE_CALLS_DEFAULT", raising=False)
    voice_control.reset_to_default()
    voice_calls._batches.clear()
    yield
    monkeypatch.delenv("VOICE_CALLS_DEFAULT", raising=False)
    voice_control.reset_to_default()
    voice_calls._batches.clear()


def user(role, name="Pau"):
    return CurrentUser(id=7, email=f"{name}@x.com", role=role, full_name=name, status="active")


# ---- the switch itself ----------------------------------------------------------------------

def test_default_is_paused():
    assert voice_control.mode() == "paused" and not voice_control.is_active()
    assert voice_control.status()["voice_calls"] == "paused"


@pytest.mark.parametrize("value", [None, "", "paused", "PAUSED", "yes", "true", "1", "on", "actve", "garbage", "activee", "active!"])
def test_invalid_or_non_active_env_is_paused(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("VOICE_CALLS_DEFAULT", value)
    voice_control.reset_to_default()
    assert voice_control.mode() == "paused"


@pytest.mark.parametrize("value", ["active", "ACTIVE", "  Active "])
def test_only_active_env_starts_active(monkeypatch, value):
    monkeypatch.setenv("VOICE_CALLS_DEFAULT", value)
    voice_control.reset_to_default()
    assert voice_control.mode() == "active"


def test_restart_returns_to_the_env_default_never_silently_active(monkeypatch):
    monkeypatch.setenv("VOICE_CALLS_DEFAULT", "paused")
    voice_control.set_mode("active", actor_name="Admin")
    assert voice_control.mode() == "active"
    voice_control.reset_to_default()  # what a process restart does
    assert voice_control.mode() == "paused" and voice_control.status()["changed_by"] is None


def test_set_mode_rejects_invalid_values_and_records_who_and_when():
    with pytest.raises(ValueError):
        voice_control.set_mode("maybe", actor_name="Admin")
    status = voice_control.set_mode(" Active ", actor_name="Pau", actor_id=7)
    assert status["voice_calls"] == "active" and status["changed_by"] == "Pau" and status["changed_at"]
    from services.audit import list_audit_log
    assert any(e["action"] == "voice_calls_resumed" and e["details"]["by"] == "Pau" for e in list_audit_log("voice_calls"))


def test_health_reports_the_mode(monkeypatch):
    import main
    monkeypatch.setitem(main.schema_state, "ok", True)  # the schema check needs a database; irrelevant here
    assert main.health()["voice_calls"] == "paused"
    voice_control.set_mode("active", actor_name="Pau")
    assert main.health()["voice_calls"] == "active"


# ---- assignment fan-out ---------------------------------------------------------------------

def _run_assign(monkeypatch, mode, provider, secret="s", place_spy=None):
    if secret:
        monkeypatch.setenv("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET", secret)
    sent_emails = []
    create_call = AsyncMock(return_value={"id": "call-1", "status": "queued"})
    monkeypatch.setattr(vapi_client, "create_call", create_call)
    result, posts = _assign(monkeypatch, voice_provider=provider, voice_mode=mode, place_spy=place_spy)
    return result, posts, create_call


@pytest.mark.parametrize("provider", ["n8n", "direct"])
def test_paused_assignment_skips_voice_but_email_whatsapp_sms_still_go(monkeypatch, caplog, provider):
    monkeypatch.setenv("JWT_SECRET", "test")
    with caplog.at_level(logging.INFO, logger="voice_control"):
        result, posts, create_call = _run_assign(monkeypatch, "paused", provider)
    create_call.assert_not_called()
    assert not any("voice-call" in url for url in posts)                     # n8n voice webhook not hit
    assert sum("whatsapp" in u for u in posts) == 1 and sum("sms" in u for u in posts) == 1
    assert result["channels"] == {"email": "queued", "whatsapp": "queued", "sms": "queued", "voice": "paused"}
    assert result["voice"] == "paused" and result["voiceByDriver"] == {"Juan": "paused"}
    assert result["success"] is True and "voice" not in result["dispatched"]    # skipped, not an error
    lines = [r.getMessage() for r in caplog.records if "[VOICE] skipped: calls paused" in r.getMessage()]
    assert lines and "SO-1001" in lines[0] and "Juan" in lines[0]
    assert "0917" not in " ".join(r.getMessage() for r in caplog.records)    # no phone numbers logged


def test_paused_assignment_does_not_even_reach_the_voice_pipeline(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test")
    calls = []
    _run_assign(monkeypatch, "paused", "direct", place_spy=lambda **kw: calls.append(kw) or [])
    assert calls == []


def test_active_direct_assignment_places_the_call(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test")
    placed = []
    result, posts, _ = _run_assign(monkeypatch, "active", "direct", place_spy=lambda **kw: placed.append(kw) or ["call-1"])
    assert len(placed) == 1 and result["channels"]["voice"] == "queued_direct" and result["voiceByDriver"] == {"Juan": "queued_direct"}


def test_active_n8n_assignment_still_hits_the_voice_webhook(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test")
    result, posts, _ = _run_assign(monkeypatch, "active", "n8n")
    assert any("voice-call" in url for url in posts) and result["channels"]["voice"] == "queued"


# ---- Vapi placement: driver calls, team call, last-line guard --------------------------------

DRIVERS = [{"id": 1, "name": "Juan Dela Cruz", "phone": PHONE}, {"id": 2, "name": "Pedro", "phone": PHONE}]
SOS = [{"soNumber": "SO-1001", "clientName": "Acme", "totalKgs": 5, "totalPacks": 2}]


def place(**extra):
    return asyncio.run(voice_calls.place_assignment_calls(assignment_id="asg-1", salesorder_ids=["1"], vehicle_id="V", truck_plate="NAN1234", warehouse="Mets", sales_orders=SOS, drivers=DRIVERS, **extra))


def test_paused_places_no_driver_calls_and_queues_nothing(monkeypatch, caplog):
    create_call = AsyncMock(return_value={"id": "c"})
    monkeypatch.setattr(vapi_client, "create_call", create_call)
    with caplog.at_level(logging.INFO, logger="voice_control"):
        assert place() == []
    create_call.assert_not_called()
    assert "asg-1" not in voice_calls._batches
    assert sum("[VOICE] skipped: calls paused" in r.getMessage() for r in caplog.records) == 2  # one per driver
    assert "0917" not in " ".join(r.getMessage() for r in caplog.records)


def test_active_places_one_call_per_driver(monkeypatch):
    voice_control.set_mode("active", actor_name="Pau")
    create_call = AsyncMock(side_effect=[{"id": "c1"}, {"id": "c2"}])
    monkeypatch.setattr(vapi_client, "create_call", create_call)
    assert place() == ["c1", "c2"] and create_call.await_count == 2


def test_no_backfill_on_resume(monkeypatch):
    create_call = AsyncMock(return_value={"id": "c"})
    monkeypatch.setattr(vapi_client, "create_call", create_call)
    place()                                         # paused: skipped
    voice_control.set_mode("active", actor_name="Pau")
    asyncio.run(voice_calls._maybe_call_team("asg-1"))
    create_call.assert_not_called()                 # nothing was queued, nothing is replayed
    assert not voice_calls._batches


def _team_batch():
    voice_calls._batches["asg-9"] = {"calls": {"c1": True}, "drivers": ["Juan"], "truckPlate": "NAN1234", "warehouse": "Mets", "vehicle": "V", "salesOrders": SOS, "teamNotified": False}


def test_team_confirmation_call_is_skipped_while_paused(monkeypatch):
    create_call = AsyncMock(return_value={"id": "t"})
    monkeypatch.setattr(vapi_client, "create_call", create_call)
    monkeypatch.setattr(staff_directory_cache, "notify_list", lambda: [{"name": "Ops", "phone": PHONE}])
    _team_batch()
    asyncio.run(voice_calls._maybe_call_team("asg-9"))
    create_call.assert_not_called()
    assert voice_calls._batches["asg-9"]["teamNotified"] is True   # not retried later either
    voice_control.set_mode("active", actor_name="Pau")
    asyncio.run(voice_calls._maybe_call_team("asg-9"))
    create_call.assert_not_called()


def test_team_confirmation_call_is_placed_when_active(monkeypatch):
    voice_control.set_mode("active", actor_name="Pau")
    create_call = AsyncMock(return_value={"id": "t"})
    monkeypatch.setattr(vapi_client, "create_call", create_call)
    monkeypatch.setattr(staff_directory_cache, "notify_list", lambda: [{"name": "Ops", "phone": PHONE}])
    _team_batch()
    asyncio.run(voice_calls._maybe_call_team("asg-9"))
    assert create_call.await_count == 1


def test_create_call_itself_refuses_while_paused_without_any_http(monkeypatch):
    request = AsyncMock(side_effect=AssertionError("Vapi must not be contacted while paused"))
    monkeypatch.setattr(vapi_client, "_request", request)
    with pytest.raises(voice_control.VoiceCallsPaused):
        asyncio.run(vapi_client.create_call({"customer": {"number": "+639171234567"}}))
    request.assert_not_called()


def test_in_progress_calls_are_not_cut_off_by_pausing():
    voice_calls._live["live-1"] = {"status": "in-progress", "assignment_id": "a"}
    voice_control.set_mode("paused", actor_name="Pau")
    assert voice_calls._live["live-1"]["status"] == "in-progress"
    voice_calls._live.pop("live-1", None)


# ---- dispatch escalation (voice channel of /api/dispatch/send) --------------------------------

def test_dispatch_voice_channel_is_skipped_while_paused(monkeypatch):
    webhook = AsyncMock(return_value=(True, "pm-1"))
    monkeypatch.setattr(dispatch, "_dispatch_webhook", webhook)
    monkeypatch.setattr(dispatch, "_send_email_direct", AsyncMock(return_value=(True, "m-1", None)))
    body = dispatch.SendMessageBody(audience="internal", recipient_name="Ops", recipient_phone=PHONE, recipient_email="ops@x.com", channels=["voice", "email"], body="Truck down", severity="critical", related_so_number="SO-1")
    result = asyncio.run(dispatch.send_message(body))
    assert webhook.await_count == 0                       # voice never reached n8n
    statuses = {m["channel"]: m["status"] for m in result["messages"]}
    assert statuses["voice"] == "skipped" and statuses["email"] == "sent"
    voice_control.set_mode("active", actor_name="Pau")
    asyncio.run(dispatch.send_message(body))
    assert webhook.await_count == 1                       # active: today's behaviour


# ---- admin endpoint -------------------------------------------------------------------------

def client_as(role):
    app = FastAPI()
    app.include_router(voice.router)
    app.include_router(voice.admin_router)
    app.dependency_overrides[get_current_user] = lambda: user(role)
    return TestClient(app)


@pytest.mark.parametrize("role", ["dispatcher", "warehouse", "driver"])
def test_non_admin_cannot_change_the_switch(role):
    response = client_as(role).put("/api/admin/voice-calls", json={"voice_calls": "active"})
    assert response.status_code == 403
    assert voice_control.mode() == "paused"


def test_admin_can_resume_and_pause_and_status_shows_who_and_when():
    admin = client_as("admin")
    response = admin.put("/api/admin/voice-calls", json={"voice_calls": "active"})
    assert response.status_code == 200 and response.json()["voice_calls"] == "active" and response.json()["changed_by"] == "Pau"
    assert voice_control.is_active()
    assert admin.put("/api/admin/voice-calls", json={"voice_calls": "paused"}).json()["voice_calls"] == "paused"
    assert admin.put("/api/admin/voice-calls", json={"voice_calls": "nonsense"}).status_code == 400
    assert voice_control.mode() == "paused"


def test_non_admin_roles_can_read_the_status():
    for role in ("dispatcher", "warehouse"):
        response = client_as(role).get("/api/voice/settings")
        assert response.status_code == 200 and response.json()["voice_calls"] == "paused"
