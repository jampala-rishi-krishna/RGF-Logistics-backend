from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.dependencies import get_current_user
from routers import zoho_webhooks


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("ZOHO_WEBHOOK_CAPTURE", "false")
    monkeypatch.setenv("ZOHO_WEBHOOKS_ENABLED", "true")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "phase1-secret")
    monkeypatch.setattr(zoho_webhooks, "DEBOUNCE_SECONDS", 30)
    zoho_webhooks.flush_pending_webhooks()
    with zoho_webhooks._lock:
        zoho_webhooks._seen.clear()
    app = FastAPI()
    app.include_router(zoho_webhooks.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="admin")
    return TestClient(app)


def test_disabled_receiver_acknowledges_without_apply(app, monkeypatch):
    app.app.dependency_overrides  # keep the app alive for the request
    monkeypatch.setenv("ZOHO_WEBHOOKS_ENABLED", "false")
    response = app.post("/api/zoho/webhooks/salesorder", headers={"X-Zoho-Webhook-Secret": "phase1-secret"}, json={"salesorder_id": "1"})
    assert response.status_code == 200
    assert response.json() == {"ignored": True}


def test_salesorder_debounce_keeps_newest(app, monkeypatch):
    seen = []
    monkeypatch.setattr(zoho_webhooks, "_apply", lambda route, record: seen.append((route, record["last_modified_time"])) or True)
    for modified in ("2026-10-22T10:00:00+0800", "2026-10-22T10:00:01+0800", "2026-10-22T09:59:59+0800"):
        response = app.post(
            "/api/zoho/webhooks/salesorder",
            headers={"X-Zoho-Webhook-Secret": "phase1-secret"},
            json={"salesorder": {"salesorder_id": "SO-1", "last_modified_time": modified}},
        )
        assert response.status_code == 200
    zoho_webhooks.flush_pending_webhooks()
    assert seen == [("salesorder", "2026-10-22T10:00:01+0800")]


def test_purchase_transfer_are_queued_and_package_is_noop(app, monkeypatch):
    applied = []
    monkeypatch.setattr(zoho_webhooks, "_apply", lambda route, record: applied.append(route) or True)
    assert app.post("/api/zoho/webhooks/purchaseorder", headers={"X-Zoho-Webhook-Secret": "phase1-secret"}, json={"purchaseorder": {"purchaseorder_id": "PO-1", "last_modified_time": "1"}}).status_code == 200
    assert app.post("/api/zoho/webhooks/transferorder", headers={"X-Zoho-Webhook-Secret": "phase1-secret"}, json={"transfer_order": {"transfer_order_id": "TO-1", "last_modified_time": "1"}}).status_code == 200
    package = app.post("/api/zoho/webhooks/package", headers={"X-Zoho-Webhook-Secret": "phase1-secret"}, json={"package_id": "PKG-1"})
    assert package.status_code == 200 and package.json()["ignored"] is True
    zoho_webhooks.flush_pending_webhooks()
    assert sorted(applied) == ["purchaseorder", "transferorder"]


def test_bad_secret_is_401_when_enabled(app):
    assert app.post("/api/zoho/webhooks/salesorder", headers={"X-Zoho-Webhook-Secret": "wrong"}, json={}).status_code == 401
