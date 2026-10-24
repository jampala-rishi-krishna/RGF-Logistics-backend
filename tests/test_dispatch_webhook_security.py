from __future__ import annotations

from fastapi.testclient import TestClient

import main
from services import memory_tables


SECRET = "dispatch-status-test-secret"


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("DISPATCH_STATUS_WEBHOOK_SECRET", SECRET)
    memory_tables.message_log._rows.clear()
    memory_tables.message_log._next_id = 1
    return TestClient(main.app)


def _headers(value: str = SECRET) -> dict[str, str]:
    return {"X-Dispatch-Webhook-Secret": value}


def test_status_webhook_requires_secret(monkeypatch):
    client = _client(monkeypatch)
    row = memory_tables.message_log.create(status="queued", provider_message_id="p-1")
    response = client.post(
        "/api/dispatch/webhooks/message-status",
        json={"message_log_id": row["id"], "status": "sent"},
    )
    assert response.status_code == 401
    assert memory_tables.message_log.get(row["id"])["status"] == "queued"


def test_status_webhook_rejects_invalid_secret(monkeypatch):
    client = _client(monkeypatch)
    row = memory_tables.message_log.create(status="queued", provider_message_id="p-1")
    response = client.post(
        "/api/dispatch/webhooks/message-status",
        headers=_headers("wrong"),
        json={"message_log_id": row["id"], "status": "sent"},
    )
    assert response.status_code == 401
    assert memory_tables.message_log.get(row["id"])["status"] == "queued"


def test_valid_callback_is_idempotent_and_does_not_replace_provider_id(monkeypatch):
    client = _client(monkeypatch)
    row = memory_tables.message_log.create(status="queued", provider_message_id="p-1")
    first = client.post(
        "/api/dispatch/webhooks/message-status",
        headers=_headers(),
        json={"message_log_id": row["id"], "status": "sent"},
    )
    replay = client.post(
        "/api/dispatch/webhooks/message-status",
        headers=_headers(),
        json={"provider_message_id": "p-1", "status": "sent"},
    )
    replacement = client.post(
        "/api/dispatch/webhooks/message-status",
        headers=_headers(),
        json={"message_log_id": row["id"], "status": "delivered", "new_provider_message_id": "p-2"},
    )
    assert first.status_code == replay.status_code == 200
    assert replacement.status_code == 409
    stored = memory_tables.message_log.get(row["id"])
    assert stored["status"] == "sent" and stored["provider_message_id"] == "p-1"


def test_status_transition_and_malformed_event_validation(monkeypatch):
    client = _client(monkeypatch)
    row = memory_tables.message_log.create(status="delivered", provider_message_id="p-1")
    backwards = client.post(
        "/api/dispatch/webhooks/message-status",
        headers=_headers(),
        json={"message_log_id": row["id"], "status": "sent"},
    )
    malformed = client.post(
        "/api/dispatch/webhooks/message-status",
        headers=_headers(),
        json={"status": "delivered"},
    )
    unknown_status = client.post(
        "/api/dispatch/webhooks/message-status",
        headers=_headers(),
        json={"message_log_id": row["id"], "status": "mystery"},
    )
    assert backwards.status_code == 409
    assert malformed.status_code == 422
    assert unknown_status.status_code == 422
    assert memory_tables.message_log.get(row["id"])["status"] == "delivered"


def test_unknown_message_and_unconfigured_secret_fail_closed(monkeypatch):
    client = _client(monkeypatch)
    unknown = client.post(
        "/api/dispatch/webhooks/message-status",
        headers=_headers(),
        json={"provider_message_id": "missing", "status": "sent"},
    )
    monkeypatch.delenv("DISPATCH_STATUS_WEBHOOK_SECRET")
    unconfigured = client.post(
        "/api/dispatch/webhooks/message-status",
        json={"provider_message_id": "missing", "status": "sent"},
    )
    assert unknown.status_code == 404
    assert unconfigured.status_code == 503

