"""Admin pause/resume for outbound WhatsApp messages. No env var: admin panel only. n8n/Twilio mocked."""
import asyncio
import logging
import os
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test")

from auth.dependencies import CurrentUser, get_current_user
from routers import dispatch, voice
from services import voice_control, whatsapp_control
from tests.test_email_conversations import _assign

PHONE = "0917 123 4567"


@pytest.fixture(autouse=True)
def clean_state():
    whatsapp_control.reset_to_default()
    voice_control.reset_to_default()
    yield
    whatsapp_control.reset_to_default()
    voice_control.reset_to_default()


def test_default_is_paused_without_any_environment_variable(monkeypatch):
    for name in list(os.environ):
        if "WHATSAPP" in name.upper():
            monkeypatch.delenv(name, raising=False)
    whatsapp_control.reset_to_default()
    assert whatsapp_control.mode() == "paused" and not whatsapp_control.is_active()


def test_restart_returns_to_default_and_records_who_and_when():
    status = whatsapp_control.set_mode(" Active ", actor_name="Pau", actor_id=7)
    assert status["whatsapp"] == "active" and status["changed_by"] == "Pau" and status["changed_at"]
    from services.audit import list_audit_log
    assert any(e["action"] == "whatsapp_resumed" and e["details"]["by"] == "Pau" for e in list_audit_log("whatsapp"))
    whatsapp_control.reset_to_default()
    assert whatsapp_control.mode() == "paused"
    with pytest.raises(ValueError):
        whatsapp_control.set_mode("maybe", actor_name="x")


def test_health_reports_both_switches(monkeypatch):
    import main
    monkeypatch.setitem(main.schema_state, "ok", True)
    body = main.health()
    assert body["whatsapp_messages"] == "paused" and body["voice_calls"] == "paused"


def run_assign(monkeypatch, whatsapp_mode):
    monkeypatch.setenv("JWT_SECRET", "test")
    monkeypatch.setenv("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET", "s")
    return _assign(monkeypatch, voice_mode="active", whatsapp_mode=whatsapp_mode)


def test_paused_assignment_skips_whatsapp_but_email_sms_and_voice_still_go(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="whatsapp_control"):
        result, posts = run_assign(monkeypatch, "paused")
    assert not any("whatsapp" in url for url in posts)
    assert sum("sms" in u for u in posts) == 1 and sum("voice-call" in u for u in posts) == 1
    assert result["channels"] == {"email": "queued", "whatsapp": "paused", "sms": "queued", "voice": "queued"}
    assert result["success"] is True and "whatsapp" not in result["dispatched"]
    lines = [r.getMessage() for r in caplog.records if "[WHATSAPP] skipped: messages paused" in r.getMessage()]
    assert lines and "SO-1001" in lines[0] and "Juan" in lines[0]
    assert "0917" not in " ".join(r.getMessage() for r in caplog.records)


def test_active_assignment_still_sends_whatsapp(monkeypatch):
    result, posts = run_assign(monkeypatch, "active")
    assert sum("whatsapp" in u for u in posts) == 1 and result["channels"]["whatsapp"] == "queued"


def test_dispatch_whatsapp_is_skipped_while_paused_but_sms_goes(monkeypatch):
    webhook = AsyncMock(return_value=(True, "pm-1"))
    monkeypatch.setattr(dispatch, "_dispatch_webhook", webhook)
    body = dispatch.SendMessageBody(audience="driver", recipient_name="Juan", recipient_phone=PHONE, channels=["whatsapp", "sms"], body="Load ready", related_so_number="SO-1")
    result = asyncio.run(dispatch.send_message(body))
    statuses = {m["channel"]: m["status"] for m in result["messages"]}
    assert statuses == {"whatsapp": "skipped", "sms": "sent"}
    assert webhook.await_count == 1                    # only SMS reached n8n
    whatsapp_control.set_mode("active", actor_name="Pau")
    asyncio.run(dispatch.send_message(body))
    assert webhook.await_count == 3                    # resumed: both channels go, nothing was backfilled earlier


def client_as(role, name="Pau"):
    app = FastAPI()
    app.include_router(voice.router)
    app.include_router(voice.admin_router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=7, email=f"{name}@x.com", role=role, full_name=name, status="active")
    return TestClient(app)


@pytest.mark.parametrize("role", ["dispatcher", "warehouse", "driver"])
def test_non_admin_cannot_change_whatsapp(role):
    assert client_as(role).put("/api/admin/whatsapp", json={"whatsapp": "active"}).status_code == 403
    assert whatsapp_control.mode() == "paused"


def test_admin_can_resume_and_pause_and_everyone_can_read_status():
    admin = client_as("admin")
    assert admin.put("/api/admin/whatsapp", json={"whatsapp": "active"}).json()["whatsapp"] == "active"
    assert client_as("dispatcher").get("/api/voice/settings").json()["whatsapp"]["whatsapp"] == "active"
    assert admin.put("/api/admin/whatsapp", json={"whatsapp": "paused"}).json()["whatsapp"] == "paused"
    assert admin.put("/api/admin/whatsapp", json={"whatsapp": "nonsense"}).status_code == 400
    assert whatsapp_control.mode() == "paused"


def test_voice_switch_is_independent_of_whatsapp():
    client_as("admin").put("/api/admin/whatsapp", json={"whatsapp": "active"})
    assert voice_control.mode() == "paused"
