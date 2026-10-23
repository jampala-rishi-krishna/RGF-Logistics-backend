from datetime import date, datetime
from zoneinfo import ZoneInfo

from services import zoho_usage


class Store:
    def __init__(self):
        self.rows = {}

    def load(self, day):
        return {(category, source): count for (stored_day, category, source), count in self.rows.items() if stored_day == day}

    def write(self, rows):
        for day, category, source, count in rows:
            key = (day, category, source)
            self.rows[key] = self.rows.get(key, 0) + count

    def history(self, days=7, end=None):
        end = end or date.today()
        start = end.fromordinal(end.toordinal() - days + 1)
        totals = {}
        for (day, category, _source), count in self.rows.items():
            if start <= day <= end:
                totals[(day, category)] = totals.get((day, category), 0) + count
        return [(day, category, count) for (day, category), count in totals.items()]


def setup_function():
    zoho_usage.set_store(Store())
    zoho_usage.set_time_override(datetime(2026, 10, 23, 9, tzinfo=ZoneInfo("Asia/Manila")))
    zoho_usage.reset()


def teardown_function():
    zoho_usage.reset()
    zoho_usage.set_store(None)
    zoho_usage.set_time_override(None)


def test_webhook_counts_survive_restart_and_history():
    store = zoho_usage._get_store()
    zoho_usage.record_webhook("salesorder", "applied")
    zoho_usage.record_webhook("package", "stale")
    zoho_usage.record_shadow("shadow_ok")
    zoho_usage.record_mapping_fix()
    assert zoho_usage.flush()
    zoho_usage.reset()
    assert zoho_usage.restore()
    snap = zoho_usage.snapshot()
    assert snap["webhook_in"] == {"salesorder": 1, "package": 1}
    assert snap["webhook_outcomes"] == {"applied": 1, "stale": 1}
    assert snap["shadow_ok"] == 1 and snap["webhook_mapping_fixes"] == 1
    assert zoho_usage.webhook_history(7)[0]["total"] == 2


def test_webhook_counts_reset_at_manila_midnight():
    zoho_usage.record_webhook("salesorder", "applied")
    assert zoho_usage.snapshot()["webhook_in"]["salesorder"] == 1
    zoho_usage.set_time_override(datetime(2026, 10, 24, 0, 1, tzinfo=ZoneInfo("Asia/Manila")))
    assert zoho_usage.snapshot()["webhook_in"] == {}
    assert zoho_usage.snapshot()["webhook_outcomes"] == {}
