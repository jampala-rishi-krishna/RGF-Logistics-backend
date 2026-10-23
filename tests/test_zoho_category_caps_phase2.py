import pytest

from services import zoho_usage


def test_items_cap_blocks_items_but_acknowledge_remains_essential():
    zoho_usage.reset()
    for _ in range(1000):
        zoho_usage.record_call("items_batch")
    with pytest.raises(zoho_usage.ZohoBudgetGuard):
        zoho_usage.enforce_budget("items_batch")
    zoho_usage.enforce_budget("acknowledge")
    snap = zoho_usage.snapshot()
    assert snap["category_caps"]["items"] == {"used": 1000, "cap": 1000}


def test_category_caps_are_independent():
    zoho_usage.reset()
    for _ in range(650):
        zoho_usage.record_call("reports")
    with pytest.raises(zoho_usage.ZohoBudgetGuard):
        zoho_usage.enforce_budget("reports")
    zoho_usage.enforce_budget("items_batch")
