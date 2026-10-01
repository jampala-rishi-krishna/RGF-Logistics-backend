"""Phase 2A acquisition-layer tests. Zoho HTTP is faked at httpx.request, so counts are
real call counts through the real client, not mocks of the layer under test."""
import os
import threading
import time
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("ZOHO_ORG_ID", "org-test")

from fastapi import HTTPException

from routers import fleet, load_planning, reports
from services import live_sales_order_cache, zoho_acquisition, zoho_client, zoho_rate_limiter


def so_body(order_id):
    return {"code": 0, "salesorder": {"salesorder_id": order_id, "salesorder_number": f"SO-{order_id}", "status": "confirmed"}}


class FakeZoho:
    """Stands in for httpx.request; records every actual HTTP call."""

    def __init__(self, delay=0.0, status=200, gate=None):
        self.calls = []
        self.delay = delay
        self.status = status
        self.gate = gate
        self.lock = threading.Lock()

    def __call__(self, method, url, headers=None, params=None, timeout=None):
        with self.lock:
            self.calls.append((method, url.split("/inventory/v1/")[1]))
        if self.gate is not None:
            self.gate.wait(5)
        if self.delay:
            time.sleep(self.delay)
        path = url.split("/inventory/v1/")[1]
        status = self.status
        body = so_body(path.split("/")[1]) if path.startswith("salesorders/") and "/" not in path[len("salesorders/"):] else {"code": 0}
        return SimpleNamespace(status_code=status, headers={}, json=lambda: body if status == 200 else {"message": "boom"})

    def count(self, method, path):
        return sum(1 for c in self.calls if c == (method, path))

    def gets(self):
        return sum(1 for m, _ in self.calls if m == "GET")


class ZohoTestCase(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(zoho_client, "get_access_token", return_value="token")
        patcher.start()
        self.addCleanup(patcher.stop)
        # Phase 2A tests are about deduplication, not pacing: use a limiter that never throttles.
        zoho_rate_limiter.reset(rate_per_minute=600000.0, safety_percent=100.0, burst=1000, max_concurrency=100)
        self.addCleanup(zoho_rate_limiter.reset)

    def install(self, fake):
        patcher = patch.object(zoho_client.httpx, "request", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    @staticmethod
    def run_threads(fn, n):
        results, errors = [None] * n, [None] * n

        def work(i):
            try:
                results[i] = fn(i)
            except BaseException as exc:  # noqa: BLE001 - recorded and asserted by tests
                errors[i] = exc
        threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            assert not t.is_alive(), "caller left unresolved (deadlock)"
        return results, errors


class CoalescingTests(ZohoTestCase):
    def test_ten_concurrent_callers_one_http_get(self):
        fake = self.install(FakeZoho(delay=0.2))
        results, errors = self.run_threads(lambda i: zoho_client.fetch_sales_order_detail("1"), 10)
        self.assertEqual(errors, [None] * 10)
        self.assertEqual(fake.count("GET", "salesorders/1"), 1)
        self.assertTrue(all(r == so_body("1") for r in results))
        self.assertEqual(len(zoho_acquisition._inflight), 0)

    def test_results_are_independent_copies(self):
        self.install(FakeZoho(delay=0.2))
        results, _ = self.run_threads(lambda i: zoho_client.fetch_sales_order_detail("1"), 3)
        results[0]["salesorder"]["status"] = "mutated"
        self.assertEqual(results[1]["salesorder"]["status"], "confirmed")
        self.assertEqual(results[2]["salesorder"]["status"], "confirmed")

    def test_distinct_resources_are_not_collapsed(self):
        fake = self.install(FakeZoho(delay=0.2))
        self.run_threads(lambda i: zoho_client.fetch_sales_order_detail(f"SO-{i % 3}"), 9)
        for order_id in ("SO-0", "SO-1", "SO-2"):
            self.assertEqual(fake.count("GET", f"salesorders/{order_id}"), 1)
        self.assertEqual(fake.gets(), 3)

    def test_different_query_params_are_not_collapsed(self):
        fake = self.install(FakeZoho(delay=0.2))
        self.run_threads(lambda i: zoho_client.fetch_sales_orders(page=i % 2 + 1, per_page=200), 4)
        self.assertEqual(fake.gets(), 2)

    def test_sequential_unscoped_calls_do_not_reuse_completed_results(self):
        fake = self.install(FakeZoho())
        zoho_client.fetch_sales_order_detail("1")
        zoho_client.fetch_sales_order_detail("1")
        self.assertEqual(fake.gets(), 2)

    def test_failure_reaches_all_callers_and_is_not_cached(self):
        fake = self.install(FakeZoho(delay=0.2, status=400))
        _, errors = self.run_threads(lambda i: zoho_client.fetch_sales_order_detail("1"), 10)
        self.assertTrue(all(isinstance(e, zoho_client.ZohoError) for e in errors))
        self.assertEqual(fake.gets(), 1)
        self.assertEqual(len(zoho_acquisition._inflight), 0)
        fake.status = 200
        self.assertEqual(zoho_client.fetch_sales_order_detail("1"), so_body("1"))
        self.assertEqual(fake.gets(), 2)

    def test_slow_request_does_not_block_unrelated_resources(self):
        gate = threading.Event()
        fake = self.install(FakeZoho(gate=gate))
        slow = threading.Thread(target=zoho_client.fetch_sales_order_detail, args=("slow",))
        slow.start()
        time.sleep(0.1)
        gate.set()  # lock must be free: a second key completes while the first is mid-flight
        zoho_client.fetch_sales_order_detail("fast")
        slow.join(5)
        self.assertEqual(fake.gets(), 2)


class GenerationTests(ZohoTestCase):
    def test_post_fences_reads_so_later_get_is_not_joined_to_stale_one(self):
        gate = threading.Event()
        fake = self.install(FakeZoho(gate=gate))
        stale = threading.Thread(target=zoho_client.fetch_sales_order_detail, args=("1",))
        stale.start()
        time.sleep(0.1)
        zoho_client._request("POST", "salesorders/1/substatus/confirmed", {})
        fresh = []
        t = threading.Thread(target=lambda: fresh.append(zoho_client.fetch_sales_order_detail("1")))
        t.start()
        time.sleep(0.1)
        gate.set()
        stale.join(5)
        t.join(5)
        self.assertEqual(fake.count("GET", "salesorders/1"), 2)
        self.assertEqual(fake.count("POST", "salesorders/1/substatus/confirmed"), 1)

    def test_stale_generation_cannot_publish(self):
        epoch = zoho_acquisition.generation()
        with zoho_acquisition.invalidation():
            pass
        with zoho_acquisition.publication(epoch) as current:
            self.assertFalse(current)
        with zoho_acquisition.publication(zoho_acquisition.generation()) as current:
            self.assertTrue(current)

    def test_stale_assigned_snapshot_is_not_published(self):
        epoch = zoho_acquisition.generation()
        live_sales_order_cache._assigned_zoho.pop("stale-1", None)
        with zoho_acquisition.invalidation():
            pass
        live_sales_order_cache.publish_zoho_data("stale-1", so_body("stale-1"), epoch)
        self.assertNotIn("stale-1", live_sales_order_cache._assigned_zoho)

    def test_post_is_never_coalesced_or_replayed(self):
        fake = self.install(FakeZoho(delay=0.1))
        self.run_threads(lambda i: zoho_client._request("POST", "salesorders/1/substatus/confirmed", {}), 3)
        self.assertEqual(fake.count("POST", "salesorders/1/substatus/confirmed"), 3)


class ScopeReuseTests(ZohoTestCase):
    def test_details_reused_within_operation_only(self):
        fake = self.install(FakeZoho())

        @zoho_acquisition.operation("test-op", reuse_details=True)
        def scoped():
            return zoho_client.fetch_sales_order_detail("1"), zoho_client.fetch_sales_order_detail("1")

        a, b = scoped()
        self.assertEqual(a, b)
        self.assertEqual(fake.gets(), 1)
        scoped()  # a new invocation is a new freshness domain
        self.assertEqual(fake.gets(), 2)

    def test_list_responses_are_never_reused(self):
        fake = self.install(FakeZoho())

        @zoho_acquisition.operation("test-op", reuse_details=True)
        def scoped():
            zoho_client.fetch_sales_orders(page=1, per_page=200)
            zoho_client.fetch_sales_orders(page=1, per_page=200)

        scoped()
        self.assertEqual(fake.gets(), 2)

    def test_scope_without_reuse_does_not_reuse(self):
        fake = self.install(FakeZoho())

        @zoho_acquisition.operation("no-reuse")
        def scoped():
            zoho_client.fetch_sales_order_detail("1")
            zoho_client.fetch_sales_order_detail("1")

        scoped()
        self.assertEqual(fake.gets(), 2)


class DrawerAndAcknowledgementTests(ZohoTestCase):
    def test_drawer_open_is_one_detail_get(self):
        fake = self.install(FakeZoho())
        with patch.object(live_sales_order_cache, "find_cached", return_value=None), \
                patch.object(live_sales_order_cache, "publish_zoho_data") as publish:
            body = load_planning.get_sales_order("1")
        self.assertEqual(fake.count("GET", "salesorders/1"), 1)
        self.assertEqual(body["salesorder"], so_body("1")["salesorder"])
        self.assertIn("delivery_status", body)
        publish.assert_called_once()

    def test_remove_acknowledgement_is_one_post_one_get_warm(self):
        fake = self.install(FakeZoho())
        cached = SimpleNamespace(order_status="acknowledged")
        with patch.object(live_sales_order_cache, "find_cached", return_value=cached), \
                patch.object(live_sales_order_cache, "publish_zoho_data"), \
                patch.object(live_sales_order_cache, "invalidate_windows"):
            body = load_planning.remove_acknowledge_sales_order_route("1")
        self.assertEqual(fake.count("POST", "salesorders/1/substatus/confirmed"), 1)
        self.assertEqual(fake.count("GET", "salesorders/1"), 1)
        self.assertEqual(len(fake.calls), 2)
        self.assertTrue(body["removed_acknowledge"])
        self.assertEqual(body["status"], "confirmed")

    def test_remove_acknowledgement_not_acknowledged_makes_no_post(self):
        fake = self.install(FakeZoho())
        with patch.object(live_sales_order_cache, "find_cached", return_value=SimpleNamespace(order_status="confirmed")):
            with self.assertRaises(HTTPException) as ctx:
                load_planning.remove_acknowledge_sales_order_route("1")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(fake.calls, [])


class FleetRefreshTests(ZohoTestCase):
    def _order(self, oid, status="assigned"):
        return SimpleNamespace(id=oid, salesorder_number=f"SO-{oid}", raw_json={"salesorder_id": oid}, assignment_status=status,
                               vehicle_id=1, synced_at=None, completed_at=None)

    def _refresh(self, orders):
        fake = self.install(FakeZoho())
        merged = []

        def hydrate_like_snapshot():
            # get_assigned_snapshot() hydrates cold snapshots via fetch_sales_order_detail.
            for order in orders:
                zoho_client.fetch_sales_order_detail(order.id)
            return orders

        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = []
        with patch.object(live_sales_order_cache, "get_assigned_snapshot", side_effect=hydrate_like_snapshot), \
                patch.object(live_sales_order_cache, "merge_zoho_payload", side_effect=lambda o, f: merged.append((o.id, f))), \
                patch.object(live_sales_order_cache, "set_assignment"), \
                patch.object(fleet, "sync_history_row") as history:
            fleet.refresh_vehicles(force=True, db=db)
        return fake, merged, history

    def test_cold_refresh_multiple_assigned_orders_one_get_each(self):
        orders = [self._order("1"), self._order("2"), self._order("3")]
        fake, merged, history = self._refresh(orders)
        self.assertEqual(Counter(p for _, p in fake.calls), Counter({"salesorders/1": 1, "salesorders/2": 1, "salesorders/3": 1}))
        self.assertEqual([m[0] for m in merged], ["1", "2", "3"])
        self.assertEqual(history.call_count, 3)  # history writes unchanged

    def test_refresh_after_refresh_still_refetches(self):
        orders = [self._order("1")]
        fake, _, _ = self._refresh(orders)
        self.assertEqual(fake.gets(), 1)
        fake2, _, _ = self._refresh([self._order("1")])
        self.assertEqual(fake2.gets(), 1)  # a new refresh is a new generation of data


class ReportCoalescingTests(ZohoTestCase):
    def setUp(self):
        super().setUp()
        reports._cache.update(payload=None, fetched_at=0.0)
        reports._cache.pop("identity", None)
        self.builds = Counter()

        def build(section=None):
            self.builds[section] += 1
            time.sleep(0.2)
            return {"section": section, "n": self.builds[section]}
        patcher = patch.object(reports, "_build_report", side_effect=build)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_three_normal_cold_requests_one_build(self):
        results, errors = self.run_threads(lambda i: reports.get_rgf_logistics_report(), 3)
        self.assertEqual(errors, [None] * 3)
        self.assertEqual(self.builds[None], 1)
        self.assertTrue(all(r == results[0] for r in results))

    def test_three_forced_requests_share_one_generation(self):
        _, errors = self.run_threads(lambda i: reports.get_rgf_logistics_report(force=True), 3)
        self.assertEqual(errors, [None] * 3)
        self.assertEqual(self.builds[None], 1)

    def test_forced_request_after_completion_really_refreshes(self):
        reports.get_rgf_logistics_report()
        reports.get_rgf_logistics_report(force=True)
        self.assertEqual(self.builds[None], 2)

    def test_warm_cache_still_served_and_force_bypasses_it(self):
        reports.get_rgf_logistics_report()
        reports.get_rgf_logistics_report()
        self.assertEqual(self.builds[None], 1)

    def test_same_section_shares_but_different_sections_stay_independent(self):
        sections = ["packages", "packages", "packages", "invoices", "invoices", "invoices"]
        _, errors = self.run_threads(lambda i: reports.get_rgf_logistics_report(section=sections[i]), 6)
        self.assertEqual(errors, [None] * 6)
        self.assertEqual(self.builds["packages"], 1)
        self.assertEqual(self.builds["invoices"], 1)

    def test_report_failure_reaches_waiters_and_is_not_cached(self):
        calls = []

        def failing(section=None):
            calls.append(1)
            time.sleep(0.2)
            raise HTTPException(502, "zoho down")
        with patch.object(reports, "_build_report", side_effect=failing):
            _, errors = self.run_threads(lambda i: reports.get_rgf_logistics_report(), 3)
        self.assertTrue(all(isinstance(e, HTTPException) for e in errors))
        self.assertEqual(len(calls), 1)
        self.assertEqual(reports._report_inflight, {})
        self.assertEqual(reports.get_rgf_logistics_report()["section"], None)

    def test_returned_payload_is_not_shared_between_callers(self):
        results, _ = self.run_threads(lambda i: reports.get_rgf_logistics_report(), 2)
        results[0]["mutated"] = True
        self.assertNotIn("mutated", results[1])
        self.assertNotIn("mutated", reports.get_rgf_logistics_report())


class ReportDetailReuseTests(ZohoTestCase):
    @staticmethod
    def _record(payload, key):
        return reports._detail_record(payload, key)

    def _buckets(self, buckets):
        """Run the three enrichment buckets as _build_report does: one operation scope."""
        @zoho_acquisition.operation("report-build", reuse_details=True)
        def run():
            return [reports._detail_map(ids, zoho_client.fetch_sales_order_detail, "sales_order", 5) for ids in buckets]
        return run()

    def test_overlapping_ids_nine_logical_five_actual(self):
        fake = self.install(FakeZoho())
        maps = self._buckets([["1", "2", "3"], ["2", "3", "4"], ["3", "4", "5"]])
        self.assertEqual(fake.gets(), 5)
        self.assertEqual(sorted(fake.calls), sorted(("GET", f"salesorders/{i}") for i in "12345"))
        self.assertEqual([sorted(m) for m in maps], [["1", "2", "3"], ["2", "3", "4"], ["3", "4", "5"]])

    def test_disjoint_ids_each_fetched_once(self):
        fake = self.install(FakeZoho())
        self._buckets([["1"], ["2"], ["3"]])
        self.assertEqual(fake.gets(), 3)

    def test_cap_applies_before_dedup_per_bucket(self):
        fake = self.install(FakeZoho())
        ids = [str(i) for i in range(70)] + [str(i) for i in range(70)] + [str(i) for i in range(100, 120)]
        maps = self._buckets([ids, ids[:5]])
        # first 80 entries = 70 unique + 10 repeats; ids past position 80 never considered
        self.assertEqual(sorted(maps[0]), sorted(str(i) for i in range(70)))
        self.assertEqual(fake.gets(), 70)
        self.assertNotIn("100", maps[0])

    def test_without_scope_buckets_do_not_reuse(self):
        fake = self.install(FakeZoho())
        for ids in (["1", "2"], ["1", "2"]):
            reports._detail_map(ids, zoho_client.fetch_sales_order_detail, "sales_order", 2)
        self.assertEqual(fake.gets(), 4)


class LoggingSafetyTests(ZohoTestCase):
    def test_logs_distinguish_events_and_never_contain_secrets(self):
        self.install(FakeZoho(delay=0.2))
        with self.assertLogs("zoho", level="INFO") as logs:
            self.run_threads(lambda i: zoho_client.fetch_sales_order_detail("1"), 3)
        text = "\n".join(logs.output)
        for kind in ("logical_request", "cache_miss", "coalesced_waiter", "http_attempt", "http_result"):
            self.assertIn(f"event={kind}", text)
        self.assertNotIn("token", text.lower().replace("access_token", ""))
        self.assertNotIn("Authorization", text)


if __name__ == "__main__":
    unittest.main()
