"""1) the assigned-SO hydration skips SOs that shipped before yesterday (no Zoho call), 2) numeric settings accept "4,600" and warn once,
3) "Weight unavailable" is logged once per item per hour. Mocked only: any Zoho call fails the test."""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("JWT_SECRET", "test-secret-not-real")

from services import app_settings, item_weight, live_sales_order_cache as cache, zoho_usage  # noqa: E402

PHT = ZoneInfo("Asia/Manila")


def today() -> date:
    return datetime.now(PHT).date()


# ---- 1. stale assigned rows ---------------------------------------------------------------------------------------------------


@pytest.fixture()
def state(monkeypatch):
    fetched: list[str] = []

    def fake_ensure(order_id):
        fetched.append(order_id)  # a stand-in for ONE Zoho SO-detail call
        row = SimpleNamespace(id=order_id)
        cache._assigned_zoho[order_id] = row
        return row

    monkeypatch.setattr(cache, "ensure_zoho_data", fake_ensure)
    monkeypatch.setattr(cache, "fetch_sales_order_detail", lambda *a, **k: (_ for _ in ()).throw(AssertionError("direct Zoho call")))
    saved_state, saved_zoho = dict(cache._assignment_state), dict(cache._assigned_zoho)
    cache._assignment_state.clear()
    cache._assigned_zoho.clear()
    cache._stale_log.update({"count": None, "at": 0.0})
    yield fetched
    cache._assignment_state.clear()
    cache._assignment_state.update(saved_state)
    cache._assigned_zoho.clear()
    cache._assigned_zoho.update(saved_zoho)


def put(order_id, shipped, status="assigned"):
    cache._assignment_state[order_id] = {"assignment_status": status, "expected_shipment_date": shipped}


def test_only_assigned_sos_from_yesterday_on_are_hydrated(state, caplog):
    t = today()
    put("today", t)
    put("future", t + timedelta(days=3))
    put("yesterday", t - timedelta(days=1))  # the grace day: still hydrated
    put("two-days-old", t - timedelta(days=2))
    put("ancient", t - timedelta(days=40))
    put("unknown-date", None)  # cannot be judged: never called stale
    put("done", t - timedelta(days=30), status="completed")  # not asked for by Fleet (statuses={"assigned"})
    with caplog.at_level(logging.INFO, logger="live_sales_order_cache"):
        rows, had_failures = cache.get_assigned_snapshot_ex(include_window=False, statuses=frozenset({"assigned"}))
    assert sorted(state) == ["future", "today", "unknown-date", "yesterday"]  # 4 detail calls instead of 6
    assert sorted(r.id for r in rows) == sorted(state) and had_failures is False
    assert any("skipped 2 assigned SO(s)" in m for m in caplog.messages)
    assert cache.stale_assigned_count() == 3  # two-days-old + ancient + the completed one: assigned-or-later rows that shipped before yesterday


def test_the_skip_is_logged_once_not_on_every_call(state, caplog):
    put("today", today())
    put("old", today() - timedelta(days=9))
    with caplog.at_level(logging.INFO, logger="live_sales_order_cache"):
        for _ in range(5):
            cache.get_assigned_snapshot_ex(include_window=False, statuses=frozenset({"assigned"}))
    assert sum("skipped 1 assigned SO(s)" in m for m in caplog.messages) == 1


def test_a_stale_row_never_triggers_a_zoho_call_even_when_nothing_is_cached(state):
    put("old-1", today() - timedelta(days=5))
    put("old-2", today() - timedelta(days=50))
    rows, had_failures = cache.get_assigned_snapshot_ex(include_window=False)
    assert rows == [] and had_failures is False and state == []


def test_state_loaded_from_history_carries_the_shipment_date():
    row = SimpleNamespace(id="x", vehicle_id="V", driver_id=1, assignment_status="assigned", assigned_at=None, assigned_by=None, completed_at=None, route_id=None, manifest_id=None, helper_ids=[],
                          email_status=None, email_error=None, email_sent_at=None, email_message_id=None, assignment_batch_id="b", expected_shipment_date=date(2026, 10, 1))

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [row]

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            return Result()

    saved = dict(cache._assignment_state)
    try:
        cache.load_assignment_state_from_history(Db())
        assert cache._assignment_state["x"]["expected_shipment_date"] == date(2026, 10, 1)
    finally:
        cache._assignment_state.clear()
        cache._assignment_state.update(saved)


# ---- 2. app settings ---------------------------------------------------------------------------------------------------------


@pytest.fixture()
def settings(monkeypatch):
    for name in ("ZOHO_DAILY_BUDGET", "ZOHO_GUARD_CACHE_ONLY_PCT", "ZOHO_FAILSAFE_SO_DETAIL_CAP", "ZOHO_ORG_DAILY_LIMIT"):
        monkeypatch.delenv(name, raising=False)
    app_settings.reset()
    yield monkeypatch
    app_settings.reset()


@pytest.mark.parametrize("raw,expected", [("4,600", 4600), (" 4 600 ", 4600), ("4 600", 4600), ("4600", 4600), ("4,6,0,0", 4600)])
def test_the_environment_value_accepts_thousands_separators(settings, raw, expected):
    settings.setenv("ZOHO_DAILY_BUDGET", raw)
    assert app_settings.get_int("ZOHO_DAILY_BUDGET") == expected
    assert app_settings.describe_one("ZOHO_DAILY_BUDGET")["source"] == "env"


def test_the_admin_form_accepts_4_600_and_still_enforces_the_bounds(settings):
    assert app_settings.parse(app_settings.DEFINITIONS["ZOHO_DAILY_BUDGET"], "4,600") == 4600
    with pytest.raises(app_settings.SettingError):
        app_settings.parse(app_settings.DEFINITIONS["ZOHO_DAILY_BUDGET"], "20,000")  # above the organisation limit
    with pytest.raises(app_settings.SettingError):
        app_settings.parse(app_settings.DEFINITIONS["ZOHO_DAILY_BUDGET"], "4.6k")
    with pytest.raises(app_settings.SettingError):
        app_settings.parse(app_settings.DEFINITIONS["ZOHO_DAILY_BUDGET"], " , ")


def test_org_limit_with_a_separator_is_read_by_both_modules(settings):
    settings.setenv("ZOHO_ORG_DAILY_LIMIT", "10,000")
    assert app_settings._org_limit() == 10000 and zoho_usage.org_limit() == 10000


def test_a_really_invalid_value_warns_once_per_startup_not_per_read(settings, caplog):
    settings.setenv("ZOHO_DAILY_BUDGET", "lots")
    with caplog.at_level(logging.WARNING, logger="app_settings"):
        values = [app_settings.get_int("ZOHO_DAILY_BUDGET") for _ in range(500)]
    assert set(values) == {4500}  # the built-in default
    assert sum("environment value for ZOHO_DAILY_BUDGET is invalid" in m for m in caplog.messages) == 1


def test_an_invalid_stored_override_warns_once(settings, caplog):
    app_settings._overrides["ZOHO_FAILSAFE_SO_DETAIL_CAP"] = "9999"  # above the maximum of 1000
    with caplog.at_level(logging.WARNING, logger="app_settings"):
        for _ in range(100):
            app_settings.get_int("ZOHO_FAILSAFE_SO_DETAIL_CAP")
    assert sum("stored value for ZOHO_FAILSAFE_SO_DETAIL_CAP is invalid" in m for m in caplog.messages) == 1


# ---- 3. item_weight log noise -------------------------------------------------------------------------------------------------


@pytest.fixture()
def weight(monkeypatch):
    item_weight._warned_at.clear()
    item_weight._cache.clear()
    clock = {"now": 1000.0}
    monkeypatch.setattr(item_weight, "_clock", lambda: clock["now"])
    monkeypatch.setattr(item_weight.item_detail_cache, "get", lambda key, allow_fetch=True: {"package_details": {}})  # an item with no package weight
    yield clock
    item_weight._warned_at.clear()
    item_weight._cache.clear()


def lines(n, item="item-1"):
    return [item_weight.calculate_line_weight_kg(2, "pck", item, context=f"SO-{i}") for i in range(n)]


def test_weight_unavailable_is_logged_once_per_item_per_hour_not_per_line(weight, caplog):
    with caplog.at_level(logging.WARNING, logger="item_weight"):
        assert lines(50) == [None] * 50
    assert sum("Weight unavailable" in m for m in caplog.messages) == 1


def test_a_different_item_logs_separately_and_the_same_item_logs_again_after_an_hour(weight, caplog):
    with caplog.at_level(logging.WARNING, logger="item_weight"):
        lines(5, "item-1")
        lines(5, "item-2")
        assert sum("Weight unavailable" in m for m in caplog.messages) == 2
        weight["now"] += 3599
        lines(5, "item-1")
        assert sum("Weight unavailable" in m for m in caplog.messages) == 2  # still inside the hour
        weight["now"] += 2
        lines(5, "item-1")
        assert sum("Weight unavailable" in m for m in caplog.messages) == 3


def test_missing_item_id_and_cache_only_reads_are_not_noisy_either(weight, caplog):
    with caplog.at_level(logging.WARNING, logger="item_weight"):
        for _ in range(20):
            item_weight.calculate_line_weight_kg(1, "pck", None, context="SO-9 SKU=A")
        for _ in range(20):
            item_weight.calculate_line_weight_kg(1, "pck", "item-3", context="list", allow_fetch=False)
    assert sum("Weight unavailable" in m for m in caplog.messages) == 1  # the missing-id case once; cache-only reads are debug
