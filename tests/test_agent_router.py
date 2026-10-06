import os
from types import SimpleNamespace

os.environ.setdefault("JWT_SECRET", "test")

from routers.agent import _function_call_input_item


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
