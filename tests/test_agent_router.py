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


def test_loopback_base_url_with_wrong_port_is_ignored_and_warned_once(monkeypatch, caplog):
    monkeypatch.setenv("INTERNAL_API_BASE_URL", "http://127.0.0.1:8003")
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setattr(agent_tools, "_warned_stale_base_url", False)

    with caplog.at_level("WARNING", logger="agent"):
        assert agent_tools._internal_api_base_url() == "http://127.0.0.1:10000"
        assert agent_tools._internal_api_base_url() == "http://127.0.0.1:10000"

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "8003" in warnings[0].getMessage()


def test_localhost_with_wrong_port_is_ignored(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_BASE_URL", "http://localhost:8003")
    monkeypatch.setenv("PORT", "10000")

    assert agent_tools._internal_api_base_url() == "http://127.0.0.1:10000"


def test_loopback_base_url_with_matching_port_and_remote_override_are_kept(monkeypatch):
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("INTERNAL_API_BASE_URL", "http://127.0.0.1:10000/")
    assert agent_tools._internal_api_base_url() == "http://127.0.0.1:10000"

    monkeypatch.setenv("INTERNAL_API_BASE_URL", "https://api.internal.example")
    assert agent_tools._internal_api_base_url() == "https://api.internal.example"


def test_default_base_url_falls_back_to_8003_without_port(monkeypatch):
    monkeypatch.delenv("INTERNAL_API_BASE_URL", raising=False)
    monkeypatch.delenv("PORT", raising=False)

    assert agent_tools._internal_api_base_url() == "http://127.0.0.1:8003"


def _run_chat_with_failing_tool(monkeypatch, replies):
    import asyncio
    import httpx
    from routers import agent as agent_router

    call = SimpleNamespace(type="function_call", call_id="c1", name="get_control_tower_fleet", arguments="{}")
    responses = iter([
        SimpleNamespace(output=[call], output_text=""),
        SimpleNamespace(output=[], output_text="Fleet data is unavailable."),
    ])
    seen_inputs = []

    async def fake_create_response(items):
        seen_inputs.append(items)
        return next(responses)

    async def failing_tool(args, token):
        raise httpx.ConnectError("All connection attempts failed")

    monkeypatch.setattr(agent_router, "create_response", fake_create_response)
    monkeypatch.setitem(agent_router.READ_TOOLS, "get_control_tower_fleet", failing_tool)

    user = SimpleNamespace(id=7, email="a@b.c", role="admin")
    creds = SimpleNamespace(credentials="SECRET-TOKEN")
    body = agent_router.ChatBody(message="give me fleet info SECRET-MESSAGE")
    result = asyncio.run(agent_router.chat(body, user, creds))
    return result, seen_inputs


def test_tool_exception_is_logged_and_returned_to_model_as_specific_error(monkeypatch, caplog):
    import json

    with caplog.at_level("INFO", logger="agent"):
        result, seen_inputs = _run_chat_with_failing_tool(monkeypatch, None)

    assert result["reply"] == "Fleet data is unavailable."

    tool_records = [r for r in caplog.records if "[AGENT_TOOL]" in r.getMessage()]
    assert len(tool_records) == 1
    assert tool_records[0].exc_info is not None

    output = [i for i in seen_inputs[1] if i.get("type") == "function_call_output"][0]
    error = json.loads(output["output"])["error"]
    assert error.startswith("get_control_tower_fleet tool failed: ConnectError")


def test_chat_summary_log_has_ids_tools_timing_and_no_content_or_token(monkeypatch, caplog):
    with caplog.at_level("INFO", logger="agent"):
        _run_chat_with_failing_tool(monkeypatch, None)

    summary = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[AGENT] request_id=")]
    assert len(summary) == 1
    line = summary[0]
    assert "user_id=7" in line
    assert "get_control_tower_fleet=error:ConnectError/" in line
    assert "mermaid=none" in line
    assert "total_ms=" in line
    assert "SECRET-TOKEN" not in caplog.text
    assert "SECRET-MESSAGE" not in caplog.text
