"""The Zoho budget guard: non-essential calls pause (cache_only) at ZOHO_GUARD_CACHE_ONLY_PCT of the daily budget
(default 92, Admin > Settings), everything except acknowledge/lock stops at 100%. The budget value itself is untouched.
Mocked only: no Zoho, no database."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("JWT_SECRET", "test-secret-not-real")

from services import app_settings, zoho_usage  # noqa: E402

BUDGET = 4500  # the real default; 86% = 3870, 92% = 4140, 100% = 4500


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("ZOHO_DAILY_BUDGET", "ZOHO_GUARD_CACHE_ONLY_PCT", "ZOHO_FAILSAFE_SO_DETAIL_CAP"):
        monkeypatch.delenv(name, raising=False)
    app_settings.reset()
    zoho_usage.reset(restored=True)
    yield
    app_settings.reset()
    zoho_usage.reset()


def use(calls: int) -> None:
    """Make today's counted total exactly `calls` (token refreshes excluded, as in production)."""
    zoho_usage.reset(restored=True)
    for _ in range(calls):
        zoho_usage.record_call("so_detail")


def blocked(feature: str) -> bool:
    try:
        zoho_usage.enforce_budget(feature)
    except zoho_usage.ZohoBudgetGuard:
        return True
    return False


def test_defaults_are_the_unchanged_budget_and_92_percent():
    assert zoho_usage.budget() == BUDGET
    assert zoho_usage.cache_only_pct() == 92


def test_6000_budget_has_exact_5750_call_guardrail(monkeypatch):
    monkeypatch.setenv("ZOHO_DAILY_BUDGET", "6000")
    value = app_settings.parse(app_settings.DEFINITIONS["ZOHO_GUARD_CACHE_ONLY_PCT"], "95.83333333333333")
    app_settings._overrides["ZOHO_GUARD_CACHE_ONLY_PCT"] = str(value)
    assert zoho_usage.cache_only_limit() == 5750
    use(5749)
    assert zoho_usage.snapshot()["guard"] == "warn" and not blocked("inventory_list")
    use(5750)
    assert zoho_usage.snapshot()["guard"] == "cache_only" and blocked("inventory_list")
    assert zoho_usage.snapshot()["budget"] == 6000 and zoho_usage.snapshot()["cache_only_limit"] == 5750
    assert not blocked("so_detail")
    use(6000)
    assert blocked("so_detail") and not blocked("acknowledge")


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", True, 49.9, 99.1])
def test_decimal_percentage_rejects_invalid_values(value):
    with pytest.raises(app_settings.SettingError):
        app_settings.parse(app_settings.DEFINITIONS["ZOHO_GUARD_CACHE_ONLY_PCT"], value)


def test_86_percent_is_allowed_exactly_the_production_situation(monkeypatch):
    use(3873)  # 3,873 / 4,500 = 86.07%: it was cache_only under the old 80% rule
    snapshot = zoho_usage.snapshot()
    assert snapshot["guard"] == "warn" and snapshot["percent_used"] == 86.1
    assert blocked("inventory_list") is False and blocked("item_detail") is False and blocked("custom_view") is False


def test_92_percent_is_cache_only_for_non_essential_calls():
    use(4140)  # exactly 92%
    assert zoho_usage.snapshot()["guard"] == "cache_only"
    for feature in ("inventory_list", "item_detail", "items_batch", "custom_view", "fleet_sync"):
        assert blocked(feature) is True, feature
    for feature in ("so_detail", "acknowledge", "lock_status"):  # essential calls still run below 100%
        assert blocked(feature) is False, feature


def test_one_call_below_92_percent_is_still_allowed():
    use(4139)
    assert zoho_usage.snapshot()["guard"] == "warn" and blocked("inventory_list") is False


def test_100_percent_is_a_hard_stop_except_for_acknowledge_and_lock():
    use(BUDGET)
    assert zoho_usage.snapshot()["guard"] == "exhausted"
    for feature in ("inventory_list", "item_detail", "custom_view", "so_detail"):  # so_detail stops too at 100%
        assert blocked(feature) is True, feature
    assert blocked("acknowledge") is False and blocked("lock_status") is False
    with pytest.raises(zoho_usage.ZohoBudgetGuard, match="exhausted"):
        zoho_usage.enforce_budget("inventory_list")


def test_the_percentage_follows_the_admin_setting(monkeypatch):
    use(3873)  # 86%
    monkeypatch.setenv("ZOHO_GUARD_CACHE_ONLY_PCT", "80")  # the old rule via the env fallback
    assert zoho_usage.cache_only_pct() == 80 and blocked("inventory_list") is True
    monkeypatch.setenv("ZOHO_GUARD_CACHE_ONLY_PCT", "95")
    assert blocked("inventory_list") is False
    app_settings._overrides["ZOHO_GUARD_CACHE_ONLY_PCT"] = "85"  # a saved admin value wins over the env
    assert zoho_usage.cache_only_pct() == 85 and blocked("inventory_list") is True
    assert zoho_usage.snapshot()["guard"] == "cache_only"


def test_a_nonsense_value_is_clamped_into_50_to_99():
    use(100)
    for raw, expected in (("10", 50), ("150", 99), ("-5", 50)):
        os.environ["ZOHO_GUARD_CACHE_ONLY_PCT"] = raw
        try:
            assert zoho_usage.cache_only_pct() == expected
        finally:
            del os.environ["ZOHO_GUARD_CACHE_ONLY_PCT"]


def test_the_budget_value_is_not_affected_by_the_percentage(monkeypatch):
    monkeypatch.setenv("ZOHO_GUARD_CACHE_ONLY_PCT", "60")
    assert zoho_usage.budget() == BUDGET
    use(2700)  # 60% of 4500
    assert zoho_usage.snapshot()["guard"] == "cache_only" and zoho_usage.snapshot()["budget"] == BUDGET


def test_a_larger_saved_budget_moves_every_threshold_with_it(monkeypatch):
    monkeypatch.setenv("ZOHO_DAILY_BUDGET", "6000")
    use(3873)  # 64.6% of 6000
    assert zoho_usage.snapshot()["guard"] == "warn" and blocked("inventory_list") is False
    use(5520)  # 92% of 6000
    assert zoho_usage.snapshot()["guard"] == "cache_only" and blocked("inventory_list") is True
