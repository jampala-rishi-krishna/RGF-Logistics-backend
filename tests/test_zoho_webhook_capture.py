import json
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from auth.dependencies import get_current_user
from routers import zoho_webhooks


@pytest.fixture()
def capture_app(monkeypatch):
    monkeypatch.setenv("ZOHO_WEBHOOK_CAPTURE", "true")
    monkeypatch.setenv("ZOHO_WEBHOOKS_ENABLED", "false")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "capture-secret")
    with zoho_webhooks._lock:
        zoho_webhooks._captures.clear()
    app = FastAPI()
    app.include_router(zoho_webhooks.router)
    app.include_router(zoho_webhooks.admin_router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="admin")
    return TestClient(app)


def test_capture_stores_redacted_body_and_safe_headers(capture_app):
    body = {
        "salesorder": {
            "salesorder_id": "SO-1", "salesorder_number": "SO26-1", "status": "confirmed",
            "shipment_date": "2026-10-22", "branch_id": "branch-1", "warehouse_name": "Mets",
            "customer_name": "Customer Secret", "customer_email": "customer@example.com",
            "contact_person": "Contact Secret", "phone": "+639171234567", "street_address": "Secret Street",
            "notes": "Private note", "line_items": [{"item_id": "item-1", "quantity": 3, "unit": "case"}],
        }
    }
    response = capture_app.post(
        "/api/zoho/webhooks/salesorder?key=capture-secret",
        headers={"Authorization": "Bearer do-not-store", "Cookie": "session=do-not-store", "X-Test": "kept"},
        json=body,
    )
    assert response.status_code == 200 and response.json() == {"captured": True}
    captured = capture_app.get("/api/admin/zoho-webhook-captures").json()["salesorder"][0]
    assert captured["content_type"].startswith("application/json")
    assert "authorization" not in {key.casefold() for key in captured["headers"]}
    assert "cookie" not in {key.casefold() for key in captured["headers"]}
    assert "x-zoho-webhook-secret" not in {key.casefold() for key in captured["headers"]}
    assert captured["headers"]["x-test"] == "kept"
    stored = captured["body"]["salesorder"]
    for key in ("customer_name", "customer_email", "contact_person", "phone", "street_address", "notes"):
        assert stored[key] == "[REDACTED]"
    assert stored["salesorder_id"] == "SO-1"
    assert stored["line_items"] == [{"item_id": "item-1", "quantity": 3, "unit": "case"}]
    assert "route" not in captured
    assert zoho_webhooks._captures["salesorder"][0]["route"] == "salesorder"


def test_all_capture_only_routes_and_form_body(capture_app):
    form = "purchaseorder_id=PO-1&status=issued&vendor_email=vendor%40example.com&quantity=2"
    for route in ("purchaseorder", "transferorder", "package", "lock-result"):
        response = capture_app.post(
            f"/api/zoho/webhooks/{route}",
            params={"key": "capture-secret"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            content=form,
        )
        assert response.status_code == 200
    stored = capture_app.get("/api/admin/zoho-webhook-captures").json()
    assert set(stored) >= {"purchaseorder", "transferorder", "package", "lock-result"}
    assert stored["purchaseorder"][0]["body"]["vendor_email"] == "[REDACTED]"
    assert stored["purchaseorder"][0]["body"]["quantity"] == "2"


def test_sixth_capture_is_not_stored_and_bad_secret_is_401(capture_app):
    for i in range(6):
        response = capture_app.post(
            "/api/zoho/webhooks/package",
            headers={"X-Zoho-Webhook-Secret": "capture-secret"},
            json={"package_id": f"PKG-{i}"},
        )
        assert response.status_code == 200
    assert len(capture_app.get("/api/admin/zoho-webhook-captures").json()["package"]) == 5
    assert capture_app.post("/api/zoho/webhooks/package", headers={"X-Zoho-Webhook-Secret": "wrong"}, json={}).status_code == 401


def test_salesorder_capture_cap_is_fifteen(capture_app):
    for i in range(16):
        response = capture_app.post(
            "/api/zoho/webhooks/salesorder",
            headers={"X-Zoho-Webhook-Secret": "capture-secret"},
            json={"salesorder_id": f"SO-{i}"},
        )
        assert response.status_code == 200
    captures = capture_app.get("/api/admin/zoho-webhook-captures").json()["salesorder"]
    assert len(captures) == 15
    assert captures[-1]["body"]["salesorder_id"] == "SO-14"


def test_non_admin_capture_listing_is_forbidden(capture_app):
    capture_app.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="dispatcher")
    assert capture_app.get("/api/admin/zoho-webhook-captures").status_code == 403


def test_capture_does_not_apply_salesorder(capture_app, monkeypatch):
    applied = []
    monkeypatch.setattr(zoho_webhooks.live_sales_order_cache, "apply_salesorder_webhook", lambda record: applied.append(record))
    assert capture_app.post("/api/zoho/webhooks/salesorder", headers={"X-Zoho-Webhook-Secret": "capture-secret"}, json={"salesorder_id": "SO-2"}).status_code == 200
    assert applied == []
