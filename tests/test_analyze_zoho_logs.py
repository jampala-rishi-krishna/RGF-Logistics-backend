"""Phase 3 tooling: logs produced by the real client must yield exact, known metrics."""
import contextvars
import importlib.util
import logging
import os
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("ZOHO_ORG_ID", "org-test")

from services import zoho_acquisition, zoho_client, zoho_rate_limiter

_spec = importlib.util.spec_from_file_location("analyze_zoho_logs", Path(__file__).resolve().parents[1] / "scripts" / "analyze_zoho_logs.py")
analyzer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analyzer)


class Capture(logging.Handler):
    """Formats exactly like main.py's basicConfig so the analyzer sees production-shaped lines."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class FakeZoho:
    def __init__(self, script=None, delay=0.0):
        self.script, self.delay, self.n, self.lock = script or {}, delay, 0, threading.Lock()

    def __call__(self, method, url, headers=None, params=None, timeout=None):
        with self.lock:
            i, self.n = self.n, self.n + 1
        if self.delay:
            import time
            time.sleep(self.delay)
        if i in self.script:
            return SimpleNamespace(status_code=self.script[i][0], headers=self.script[i][1], json=lambda: {"message": "x"})
        path = url.split("/inventory/v1/")[1]
        body = {"code": 0, "salesorder": {"salesorder_id": path.split("/")[-1]}} if path.count("/") == 1 else {"code": 0, "salesorders": [{}, {}], "page_context": {"has_more_page": True}}
        return SimpleNamespace(status_code=200, headers={}, json=lambda: body)


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        zoho_rate_limiter.reset(rate_per_minute=600000.0, safety_percent=100.0, burst=1000, max_concurrency=100)
        self.addCleanup(zoho_rate_limiter.reset)
        for target, kwargs in ((zoho_client, {"get_access_token": "t"}),):
            patch.object(zoho_client, "get_access_token", return_value="t").start()
        patch.object(zoho_client, "_sleep", lambda s: None).start()
        self.addCleanup(patch.stopall)
        self.handler = Capture()
        self.logger = logging.getLogger("zoho")
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)
        self.addCleanup(self.logger.removeHandler, self.handler)

    def run_scenario(self, fake, work):
        with patch.object(zoho_client.httpx, "request", fake):
            work()
        return analyzer.analyze(self.handler.lines)

    def test_coalescing_429_retry_and_features_are_counted_exactly(self):
        def work():
            with zoho_acquisition.request_context("/api/load-planning/inventory/sales-orders/123456789"):
                threads = [threading.Thread(target=contextvars.copy_context().run, args=(zoho_client.fetch_sales_order_detail, "123456789")) for _ in range(10)]
                [t.start() for t in threads]
                [t.join(10) for t in threads]
        result = self.run_scenario(FakeZoho({0: (429, {"Retry-After": "Wed, 01 Jan 2099 00:00:00 GMT"})}, delay=0.2), work)
        e = result["executive"]
        self.assertEqual((e["logical_requests"], e["http_attempts"], e["coalesced_waiters"]), (10, 2, 9))
        self.assertEqual((e["http_429"], e["retry_attempts"], e["cache_misses"]), (1, 1, 1))
        self.assertEqual(e["coalescing_ratio_pct"], 90.0)
        self.assertEqual(result["retries_by_cause"], {"429": 1})
        self.assertEqual(result["attempt_outcomes"], {"rate_limited": 1, "ok": 1})
        self.assertEqual(result["per_feature"]["Inventory drawer/ack"]["http"], 2)
        self.assertEqual(result["per_endpoint"]["salesorders:detail"]["http"], 2)  # HTTP-date Retry-After did not break parsing

    def test_duplicate_initial_fetches_within_one_request_are_detected(self):
        def work():
            with zoho_acquisition.request_context("/api/load-planning/inventory/sales-orders/1"):
                zoho_client.fetch_sales_order_detail("1")
                zoho_client.fetch_sales_order_detail("1")  # the Phase 2A-fixed pattern, if it ever returned
            with zoho_acquisition.request_context("/api/load-planning/inventory/sales-orders/2"):
                zoho_client.fetch_sales_order_detail("2")
        result = self.run_scenario(FakeZoho(), work)
        self.assertEqual(result["duplicate_initial_fetches_within_one_request"]["count"], 1)

    def test_scheduler_runs_get_separate_ids_and_reuse_shows_as_cache_hit(self):
        @zoho_acquisition.operation("fleet-refresh", reuse_details=True)
        def run():
            zoho_client.fetch_sales_order_detail("1")
            zoho_client.fetch_sales_order_detail("1")

        result = self.run_scenario(FakeZoho(), lambda: (run(), run()))
        e = result["executive"]
        self.assertEqual((e["logical_requests"], e["http_attempts"], e["cache_hits"]), (4, 2, 2))
        self.assertEqual(result["duplicate_initial_fetches_within_one_request"]["count"], 0)
        self.assertIn("Fleet (scheduler)", result["per_feature"])

    def test_list_metadata_waits_and_errors(self):
        def work():
            with zoho_acquisition.request_context("/api/reports/rgf-logistics"):
                zoho_client.fetch_sales_orders(page=1, per_page=200)
                with self.assertRaises(zoho_client.ZohoError):
                    zoho_client.fetch_sales_order_detail("bad")
        result = self.run_scenario(FakeZoho({1: (404, {})}), work)
        self.assertIn("salesorders:list", result["per_endpoint"])
        self.assertEqual(result["attempt_outcomes"], {"ok": 1, "client_error": 1})
        self.assertEqual(result["executive"]["terminal_failures"], 1)
        self.assertIn("has_more_page=True", "\n".join(self.handler.lines))
        self.assertEqual(set(result["rate_wait_ms"]), {"avg", "p50", "p95", "max"})
        self.assertEqual(result["per_feature"]["Reports"]["http"], 2)

    def test_logs_carry_required_fields_and_no_secrets(self):
        with zoho_acquisition.request_context("/api/fleet/vehicles/refresh"):
            self.run_scenario(FakeZoho(), lambda: zoho_client.fetch_sales_order_detail("1"))
        text = "\n".join(self.handler.lines)
        for needle in ("event=http_attempt", "route=/api/fleet/vehicles/refresh", "request_id=", "logical_id=", "endpoint=salesorders/1",
                       "attempt=1", "retry=0", "rate_wait_ms=", "concurrency_wait_ms=", "event=http_outcome", "category=ok", "ms=",
                       "event=cache_miss", "owner_id="):
            self.assertIn(needle, text)
        self.assertNotIn("oauthtoken", text.lower())
        self.assertNotIn("Authorization", text)
        self.assertNotIn("organization_id", text)

    def test_per_endpoint_logical_and_coalesced_counts(self):
        def work():
            with zoho_acquisition.request_context("/api/reports/rgf-logistics"):
                threads = [threading.Thread(target=contextvars.copy_context().run, args=(zoho_client.fetch_sales_order_detail, "55")) for _ in range(5)]
                [t.start() for t in threads]
                [t.join(10) for t in threads]
        result = self.run_scenario(FakeZoho(delay=0.2), work)
        ep = result["per_endpoint"]["salesorders:detail"]
        self.assertEqual((ep["logical"], ep["http"], ep["coalesced"]), (5, 1, 4))

    def test_request_context_reaches_sync_route_through_real_middleware(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import main
        app = FastAPI()
        app.middleware("http")(main.zoho_request_context)

        @app.get("/api/load-planning/inventory/sales-orders/{oid}")
        def route(oid: str):  # sync handler, runs in a worker thread like the real routes
            return zoho_client.fetch_sales_order_detail(oid)
        with patch.object(zoho_client.httpx, "request", FakeZoho()):
            client = TestClient(app)
            client.get("/api/load-planning/inventory/sales-orders/1?search=secretquery")
            client.get("/api/load-planning/inventory/sales-orders/2")
        text = " | ".join(self.handler.lines)
        self.assertNotIn("route=background", text)
        self.assertNotIn("secretquery", text)  # query string is never logged
        ids = {l.split("request_id=")[1].split()[0] for l in self.handler.lines if "event=http_attempt" in l}
        self.assertEqual(len(ids), 2)
        self.assertEqual(analyzer.analyze(self.handler.lines)["per_feature"]["Inventory drawer/ack"]["http"], 2)

    def test_scheduled_fleet_sync_runs_get_distinct_request_ids(self):
        from unittest.mock import MagicMock
        from routers import fleet
        from services import live_sales_order_cache
        order = SimpleNamespace(id="1", salesorder_number="SO-1", raw_json={"salesorder_id": "1"}, assignment_status="assigned",
                                vehicle_id=1, synced_at=None, completed_at=None)
        session = MagicMock()
        session.__enter__.return_value = session
        session.execute.return_value.scalars.return_value.all.return_value = []
        with patch.object(zoho_client.httpx, "request", FakeZoho()), patch.object(fleet, "SessionLocal", return_value=session),                 patch.object(live_sales_order_cache, "get_assigned_snapshot", return_value=[order]),                 patch.object(live_sales_order_cache, "merge_zoho_payload"), patch.object(live_sales_order_cache, "set_assignment"),                 patch.object(fleet, "sync_history_row"):
            fleet.scheduled_fleet_sync()
            fleet.scheduled_fleet_sync()
        attempts = [l for l in self.handler.lines if "event=http_attempt" in l]
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all("route=background" in l and "source=fleet-refresh" in l for l in attempts))
        self.assertEqual(len({l.split("request_id=")[1].split()[0] for l in attempts}), 2)

    def test_date_window_and_unparseable_lines_are_ignored(self):
        lines = ["garbage", "2026-10-01 10:00:00,123 zoho INFO [ZOHO_ACQUIRE] event=logical_request source=x route=/api/fleet request_id=a logical_id=1 generation=0",
                 "2026-10-09 10:00:00,123 zoho INFO [ZOHO_ACQUIRE] event=logical_request source=x route=/api/fleet request_id=a logical_id=2 generation=0"]
        self.assertEqual(analyzer.analyze(lines, since="2026-10-01", until="2026-10-07")["executive"]["logical_requests"], 1)


if __name__ == "__main__":
    unittest.main()
