from types import SimpleNamespace
from unittest.mock import patch

from routers import reports


def test_report_opens_and_sections_share_one_full_build():
    reports._cache.clear()
    reports._report_inflight.clear()
    reports._last_force_at = None
    builds = []
    with patch.object(reports, "_build_report", side_effect=lambda: builds.append(1) or {"kpis": {}, "errors": {}}):
        for _ in range(3):
            reports.get_rgf_logistics_report(current_user=SimpleNamespace(role="dispatcher"))
        reports.get_rgf_logistics_report(section="invoices", current_user=SimpleNamespace(role="dispatcher"))
        reports.get_rgf_logistics_report(section="packages", current_user=SimpleNamespace(role="dispatcher"))
    assert len(builds) == 1


def test_force_refresh_is_limited_to_once_per_fifteen_minutes():
    reports._cache.clear()
    reports._report_inflight.clear()
    reports._last_force_at = None
    builds = []
    with patch.object(reports, "_build_report", side_effect=lambda: builds.append(1) or {"kpis": {}, "errors": {}}), patch.object(reports.time, "time", return_value=1000.0):
        reports.get_rgf_logistics_report(force=True, current_user=SimpleNamespace(role="admin"))
        second = reports.get_rgf_logistics_report(force=True, current_user=SimpleNamespace(role="admin"))
    assert len(builds) == 1
    assert "next_refresh_allowed_at" in second
