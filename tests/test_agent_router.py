import os
from types import SimpleNamespace

os.environ.setdefault("JWT_SECRET", "test")

from routers.agent import _function_call_input_item
from services import agent_tools


def test_function_call_input_item_does_not_leak_sdk_async_field():
    call = SimpleNamespace(
        type="function_call",
        call_id="call_123",
        name="get_control_tower_fleet",
        arguments='{"date":"2026-10-06"}',
        async_=False,
    )

    item = _function_call_input_item(call)

    assert item == {
        "type": "function_call",
        "call_id": "call_123",
        "name": "get_control_tower_fleet",
        "arguments": '{"date":"2026-10-06"}',
    }
    assert "async_" not in item


def test_internal_api_base_url_uses_render_port_when_not_configured(monkeypatch):
    monkeypatch.delenv("INTERNAL_API_BASE_URL", raising=False)
    monkeypatch.setenv("PORT", "10000")

    assert agent_tools._internal_api_base_url() == "http://127.0.0.1:10000"


def test_internal_api_base_url_ignores_blank_config(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_BASE_URL", " ")
    monkeypatch.setenv("PORT", "10000")

    assert agent_tools._internal_api_base_url() == "http://127.0.0.1:10000"
