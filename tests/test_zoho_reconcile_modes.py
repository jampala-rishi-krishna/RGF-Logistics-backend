from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from services import live_sales_order_cache as lc


def _exercise(monkeypatch, *, enabled: str, shadow: str, elapsed: float):
    key = (date(2026, 10, 1).isoformat(), date(2026, 10, 16).isoformat())
    rows = {"so-1": lc._build_transient({
        "salesorder_id": "so-1", "shipment_date": "2026-10-10",
        "last_modified_time": "2026-10-09T00:00:00+00:00", "line_items": [],
    })}
    calls = []
    monkeypatch.setenv("ZOHO_WEBHOOKS_ENABLED", enabled)
    monkeypatch.setenv("ZOHO_WEBHOOK_SHADOW", shadow)
    monkeypatch.setattr(lc, "RECONCILE_SECONDS", 3600)
    monkeypatch.setattr(lc, "_wide_bounds", lambda: (date(2026, 10, 1), date(2026, 10, 16)))
    monkeypatch.setattr(lc.time, "monotonic", lambda: 100.0 + elapsed)
    monkeypatch.setattr(lc, "_pull_window", lambda start, end: calls.append(("full", start, end)) or {})
    monkeypatch.setattr(lc, "_pull_delta_since", lambda start, end, since: calls.append(("delta", start, end, since)) or {})
    monkeypatch.setattr(lc, "_hydrate_details", lambda raw: None)
    lc._windows.clear()
    lc._windows[key] = (100.0, rows)
    fixed_now = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    lc._wide_window_last_sync = fixed_now - timedelta(seconds=elapsed)
    lc._wide_window_last_full = 100.0
    with patch.object(lc, "datetime", wraps=datetime) as dt:
        dt.now.return_value = fixed_now
        lc.refresh_shared_window()
    return calls


def test_reconcile_mode_waits_for_interval(monkeypatch):
    assert _exercise(monkeypatch, enabled="true", shadow="false", elapsed=10) == []
    assert _exercise(monkeypatch, enabled="true", shadow="false", elapsed=3601)


def test_shadow_mode_keeps_old_delta_cadence(monkeypatch):
    calls = _exercise(monkeypatch, enabled="true", shadow="true", elapsed=10)
    assert calls and calls[0][0] == "delta"


def test_webhooks_disabled_keeps_rollback_cadence(monkeypatch):
    calls = _exercise(monkeypatch, enabled="false", shadow="false", elapsed=10)
    assert calls and calls[0][0] == "delta"
