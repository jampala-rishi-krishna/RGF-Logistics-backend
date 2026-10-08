"""Phase 2B: org-level concurrency/pacing, 429 + Retry-After, retry coordination.
Zoho HTTP is faked at httpx.request; backoff sleeps are recorded instead of slept."""
import os
import threading
import time
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("ZOHO_ORG_ID", "org-test")

import httpx

from routers import fleet
from services import live_sales_order_cache, zoho_client, zoho_rate_limiter
from services.zoho_rate_limiter import ZohoRateLimiter, metrics


def so_body(order_id):
    return {"code": 0, "salesorder": {"salesorder_id": order_id, "status": "confirmed"}}


class ScriptedZoho:
    """Fake httpx.request. `script` maps call-number -> response/exception; default is 200."""

    def __init__(self, script=None, delay=0.0):
        self.script = script or {}
        self.delay = delay
        self.calls = []
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.starts = []

    def __call__(self, method, url, headers=None, params=None, timeout=None):
        path = url.split("/inventory/v1/")[1]
        with self.lock:
            n = len(self.calls)
            self.calls.append((method, path))
            self.starts.append(time.monotonic())
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            outcome = self.script.get(n) or self.script.get("default")
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                status, headers_ = outcome
                return SimpleNamespace(status_code=status, headers=headers_, json=lambda: {"message": "x"})
            oid = path.split("/")[1] if path.count("/") == 1 else ""
            return SimpleNamespace(status_code=200, headers={}, json=lambda: so_body(oid))
        finally:
            with self.lock:
                self.in_flight -= 1

    def gets(self, path=None):
        return sum(1 for m, p in self.calls if m == "GET" and (path is None or p == path))


class Base(unittest.TestCase):
    def setUp(self):
        for target, value in (("get_access_token", "token"),):
            p = patch.object(zoho_client, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.sleeps = []
        p = patch.object(zoho_client, "_sleep", side_effect=self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)
        self.configure()
        self.addCleanup(zoho_rate_limiter.reset)

    def configure(self, **overrides):
        cfg = dict(rate_per_minute=600000.0, safety_percent=100.0, burst=1000, max_concurrency=100)
        cfg.update(overrides)
        zoho_rate_limiter.reset(**cfg)

    def install(self, fake):
        p = patch.object(zoho_client.httpx, "request", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    @staticmethod
    def run_threads(fn, n, join=30):
        results, errors = [None] * n, [None] * n

        def work(i):
            try:
                results[i] = fn(i)
            except BaseException as exc:  # noqa: BLE001
                errors[i] = exc
        threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(join)
            assert not t.is_alive(), "deadlock: caller never finished"
        return results, errors

    def limiter(self):
        return zoho_rate_limiter.limiter_for("org-test")


class ConcurrencyTests(Base):
    def test_concurrency_limit_respected_and_all_complete(self):
        self.configure(max_concurrency=4)
        fake = self.install(ScriptedZoho(delay=0.05))
        results, errors = self.run_threads(lambda i: zoho_client.fetch_sales_order_detail(f"SO-{i}"), 40)
        self.assertEqual(errors, [None] * 40)
        self.assertEqual(fake.max_in_flight, 4)  # reached the limit, never exceeded it
        self.assertEqual(metrics.snapshot()["max_concurrency"], 4)
        self.assertEqual(self.limiter().in_flight, 0)
        self.assertEqual([r["salesorder"]["salesorder_id"] for r in results], [f"SO-{i}" for i in range(40)])

    def test_different_resources_run_in_parallel_not_serialized(self):
        self.configure(max_concurrency=4)
        fake = self.install(ScriptedZoho(delay=0.2))
        started = time.monotonic()
        self.run_threads(lambda i: zoho_client.fetch_sales_order_detail(f"SO-{i}"), 4)
        self.assertLess(time.monotonic() - started, 0.6)  # serialized would be >= 0.8
        self.assertEqual(fake.max_in_flight, 4)

    def test_concurrency_is_shared_across_features(self):
        """GET list, GET detail and POST all draw from the same slots."""
        self.configure(max_concurrency=2)
        fake = self.install(ScriptedZoho(delay=0.1))
        fns = [lambda: zoho_client.fetch_sales_orders(page=1, per_page=200),
               lambda: zoho_client.fetch_sales_order_detail("1"),
               lambda: zoho_client._request("POST", "salesorders/1/substatus/confirmed", {}),
               lambda: zoho_client.fetch_sales_order_detail("2"),
               lambda: zoho_client.fetch_item_detail("9")]
        _, errors = self.run_threads(lambda i: fns[i](), 5)
        self.assertEqual(errors, [None] * 5)
        self.assertEqual(fake.max_in_flight, 2)

    def test_slot_released_on_every_failure_mode(self):
        self.configure(max_concurrency=1)
        for outcome in (httpx.ReadTimeout("t"), httpx.ConnectError("c"), (400, {}), (401, {}), (404, {})):
            fake = self.install(ScriptedZoho(script={"default": outcome}))
            with self.assertRaises(zoho_client.ZohoError):
                zoho_client.fetch_sales_order_detail("x")
            self.assertEqual(self.limiter().in_flight, 0)
        self.install(ScriptedZoho())  # capacity still available after all of the above
        self.assertEqual(zoho_client.fetch_sales_order_detail("ok")["salesorder"]["salesorder_id"], "ok")

    def test_exception_inside_gate_releases_capacity(self):
        limiter = ZohoRateLimiter(600000.0, 100.0, 1000, 1)
        with self.assertRaises(RuntimeError):
            with limiter.admit():
                raise RuntimeError("boom")
        with limiter.admit():  # would deadlock if the slot leaked
            pass
        self.assertEqual(limiter.in_flight, 0)


class PacingTests(Base):
    def test_request_starts_are_paced_after_burst(self):
        self.configure(rate_per_minute=1200.0, safety_percent=100.0, burst=3, max_concurrency=20)  # 50 ms spacing
        fake = self.install(ScriptedZoho())
        started = time.monotonic()
        self.run_threads(lambda i: zoho_client.fetch_sales_order_detail(f"SO-{i}"), 13)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.40)  # 3 immediate + 10 more at >=50 ms each (minus timer slop)
        starts = sorted(fake.starts)
        later = [b - a for a, b in zip(starts[3:], starts[4:])]
        self.assertGreaterEqual(min(later), 0.03)

    def test_limiter_unit_spacing_with_fake_clock(self):
        now = [100.0]
        slept = []
        limiter = ZohoRateLimiter(100.0, 50.0, 2, 5, clock=lambda: now[0], sleep=slept.append)  # 1.2 s spacing, burst 2
        for _ in range(5):
            with limiter.admit():
                pass
        self.assertEqual(slept, [approx(1.2), approx(2.4), approx(3.6)])
        now[0] += 1000  # idle: burst refills, no wait
        slept.clear()
        with limiter.admit():
            pass
        self.assertEqual(slept, [])

    def test_sustained_rate_stays_under_zoho_ceiling(self):
        limiter = ZohoRateLimiter(100.0, 80.0, 10, 5, clock=lambda: 0.0, sleep=lambda s: None)
        self.assertAlmostEqual(limiter.interval, 0.75)
        # 10 burst + 60s/0.75s = 80 more -> at most 90 starts in any 60 s window (< 100)
        self.assertLess(limiter.burst + 60.0 / limiter.interval, 100)

    def test_coalesced_waiters_do_not_consume_slots(self):
        fake = self.install(ScriptedZoho(delay=0.2))
        self.run_threads(lambda i: zoho_client.fetch_sales_order_detail("same"), 10)
        snap = metrics.snapshot()
        self.assertEqual(fake.gets(), 1)
        self.assertEqual(snap["http_attempts"], 1)
        self.assertEqual(snap["logical_requests"], 10)
        self.assertEqual(snap["coalesced_waiters"], 9)
        self.assertEqual(snap["cache_misses"], 1)

    def test_invalid_env_config_falls_back_to_defaults(self):
        with patch.dict(os.environ, {"ZOHO_API_RATE_PER_MINUTE": "abc", "ZOHO_MAX_CONCURRENCY": "0", "ZOHO_RATE_BURST": "7"}):
            cfg = zoho_rate_limiter.config_from_env()
        self.assertEqual(cfg["rate_per_minute"], 100.0)
        self.assertEqual(cfg["max_concurrency"], 5)
        self.assertEqual(cfg["burst"], 7)
        self.assertEqual(zoho_rate_limiter.config_from_env()["safety_percent"], 80.0)


def approx(value):
    class _Approx(float):
        def __eq__(self, other):
            return abs(other - value) < 1e-6
    return _Approx(value)


class RetryTests(Base):
    def test_429_retry_after_respected_through_limiter_one_logical_acquisition(self):
        fake = self.install(ScriptedZoho(script={0: (429, {"Retry-After": "2"})}, delay=0.1))
        results, errors = self.run_threads(lambda i: zoho_client.fetch_sales_order_detail("1"), 10)
        self.assertEqual(errors, [None] * 10)
        self.assertTrue(all(r["salesorder"]["salesorder_id"] == "1" for r in results))
        self.assertEqual(fake.gets(), 2)  # owner only: 429 then success; waiters did not retry
        self.assertEqual(self.sleeps, [2.0])
        snap = metrics.snapshot()
        self.assertEqual(snap["logical_requests"], 10)
        self.assertEqual(snap["http_attempts"], 2)  # both attempts passed the limiter
        self.assertEqual(snap["http_429"], 1)
        self.assertEqual(snap["retry_attempts"], 1)
        self.assertEqual(snap["successful_attempts"], 1)

    def test_429_applies_org_wide_cooldown(self):
        limiter = ZohoRateLimiter(600000.0, 100.0, 1000, 5)
        limiter.penalize(0.3)
        started = time.monotonic()
        with limiter.admit() as ticket:
            pass
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertGreaterEqual(ticket.rate_wait_ms, 250)

    def test_429_without_retry_after_uses_bounded_jittered_backoff(self):
        self.install(ScriptedZoho(script={0: (429, {}), 1: (429, {})}))
        with self.assertLogs("zoho", level="INFO") as logs:
            zoho_client.fetch_sales_order_detail("1")
        self.assertEqual(len(self.sleeps), 2)
        self.assertTrue(0.5 <= self.sleeps[0] <= 0.5 * 1.25 + 1e-9)
        self.assertTrue(1.0 <= self.sleeps[1] <= 1.0 * 1.25 + 1e-9)
        self.assertIn("retry_after=missing", "\n".join(logs.output))

    def test_retry_after_is_clamped_and_http_date_supported(self):
        self.assertEqual(zoho_client._retry_after_seconds("3"), 3.0)
        self.assertIsNone(zoho_client._retry_after_seconds("soon"))
        self.assertIsNone(zoho_client._retry_after_seconds(None))
        future = "Wed, 01 Jan 2099 00:00:00 GMT"
        self.assertGreater(zoho_client._retry_after_seconds(future), 1000)
        self.install(ScriptedZoho(script={0: (429, {"Retry-After": "999"}), 1: (429, {"Retry-After": "0"})}))
        zoho_client.fetch_sales_order_detail("1")
        self.assertEqual(self.sleeps, [10.0, 0.25])

    def test_jitter_desynchronizes_callers(self):
        delays = {round(zoho_client._fallback_delay(1), 6) for _ in range(50)}
        self.assertGreater(len(delays), 10)
        self.assertTrue(all(1.0 <= d <= 1.25 for d in delays))

    def test_5xx_timeout_and_connection_failure_retry_then_succeed(self):
        for first in ((503, {}), httpx.ReadTimeout("t"), httpx.ConnectError("c")):
            self.sleeps.clear()
            fake = self.install(ScriptedZoho(script={0: first}))
            self.assertEqual(zoho_client.fetch_sales_order_detail("1")["salesorder"]["salesorder_id"], "1")
            self.assertEqual(fake.gets(), 2)
            self.assertEqual(len(self.sleeps), 1)

    def test_retry_exhaustion_is_bounded_cleans_up_and_is_not_cached(self):
        fake = self.install(ScriptedZoho(script={"default": (429, {"Retry-After": "1"})}, delay=0.05))
        _, errors = self.run_threads(lambda i: zoho_client.fetch_sales_order_detail("1"), 10)
        self.assertTrue(all(isinstance(e, zoho_client.ZohoError) for e in errors))
        self.assertEqual(fake.gets(), zoho_client.MAX_RETRIES + 1)  # existing policy: 1 + 3 retries
        self.assertEqual(len(self.sleeps), zoho_client.MAX_RETRIES)
        self.assertEqual(self.limiter().in_flight, 0)
        from services import zoho_acquisition
        self.assertEqual(len(zoho_acquisition._inflight), 0)
        fake.script = {}
        self.assertEqual(zoho_client.fetch_sales_order_detail("1")["salesorder"]["salesorder_id"], "1")

    def test_client_errors_are_not_retried(self):
        for status in (400, 401, 403, 404):
            fake = self.install(ScriptedZoho(script={"default": (status, {})}))
            with self.assertRaises(zoho_client.ZohoError):
                zoho_client.fetch_sales_order_detail("1")
            self.assertEqual(fake.gets(), 1)
        self.assertEqual(self.sleeps, [])

    def test_backoff_sleep_holds_no_concurrency_slot(self):
        self.configure(max_concurrency=1)
        in_flight_during_sleep = []
        p = patch.object(zoho_client, "_sleep", side_effect=lambda s: in_flight_during_sleep.append(self.limiter().in_flight))
        p.start()
        self.addCleanup(p.stop)
        self.install(ScriptedZoho(script={0: (503, {})}))
        zoho_client.fetch_sales_order_detail("1")
        self.assertEqual(in_flight_during_sleep, [0])


class WriteTests(Base):
    def test_posts_stay_independent_and_are_rate_limited(self):
        fake = self.install(ScriptedZoho(delay=0.05))
        _, errors = self.run_threads(lambda i: zoho_client._request("POST", "salesorders/1/substatus/confirmed", {}), 5)
        self.assertEqual(errors, [None] * 5)
        self.assertEqual(sum(1 for m, _ in fake.calls if m == "POST"), 5)
        self.assertEqual(metrics.snapshot()["http_attempts"], 5)  # writes pass the same gate

    def test_post_waits_for_pacing(self):
        self.configure(rate_per_minute=1200.0, safety_percent=100.0, burst=1, max_concurrency=5)
        fake = self.install(ScriptedZoho())
        for _ in range(3):
            zoho_client._request("POST", "salesorders/1/substatus/confirmed", {})
        self.assertGreaterEqual(fake.starts[2] - fake.starts[0], 0.07)

    def test_post_retry_policy_unchanged(self):
        """Pre-existing behaviour, preserved on purpose: 5xx on a write is retried (see report risks)."""
        fake = self.install(ScriptedZoho(script={0: (503, {})}))
        zoho_client._request("POST", "salesorders/1/substatus/confirmed", {})
        self.assertEqual(sum(1 for m, _ in fake.calls if m == "POST"), 2)


class BurstTests(Base):
    def test_large_burst_of_distinct_requests_is_responsive_and_lossless(self):
        self.configure(rate_per_minute=6000.0, safety_percent=100.0, burst=10, max_concurrency=5)  # 10 ms spacing
        fake = self.install(ScriptedZoho(delay=0.01))
        started = time.monotonic()
        results, errors = self.run_threads(lambda i: zoho_client.fetch_sales_order_detail(f"SO-{i % 120}"), 120)
        self.assertEqual(errors, [None] * 120)
        self.assertEqual([r["salesorder"]["salesorder_id"] for r in results], [f"SO-{i}" for i in range(120)])
        self.assertLessEqual(fake.max_in_flight, 5)
        self.assertEqual(fake.gets(), 120)  # no duplicate retries
        self.assertGreaterEqual(time.monotonic() - started, 0.9)  # paced, not instantaneous
        self.assertEqual(self.limiter().in_flight, 0)

    def test_burst_with_mixed_failures_propagates_per_caller(self):
        self.configure(max_concurrency=5)
        fake = self.install(ScriptedZoho(delay=0.01))
        original = fake.__call__

        def flaky(method, url, **kw):
            if url.endswith("/SO-bad"):
                return SimpleNamespace(status_code=400, headers={}, json=lambda: {"message": "bad"})
            return original(method, url, **kw)
        self.install(flaky)
        ids = [f"SO-{i}" for i in range(30)] + ["SO-bad"] * 5
        results, errors = self.run_threads(lambda i: zoho_client.fetch_sales_order_detail(ids[i]), len(ids))
        self.assertEqual([e is not None for e in errors], [False] * 30 + [True] * 5)
        self.assertEqual(self.limiter().in_flight, 0)


class TokenRefreshTests(unittest.TestCase):
    def test_concurrent_expired_token_triggers_one_refresh(self):
        calls = []
        zoho_client._access_token, zoho_client._access_token_expires_at = None, 0.0

        def fake_post(url, data=None, timeout=None):
            calls.append(url)
            time.sleep(0.2)
            return SimpleNamespace(status_code=200, json=lambda: {"access_token": "t", "expires_in": 3600})
        env = {"ZOHO_CLIENT_ID": "i", "ZOHO_CLIENT_SECRET": "s", "ZOHO_REFRESH_TOKEN": "r"}
        with patch.dict(os.environ, env), patch.object(zoho_client.httpx, "post", fake_post):
            threads = [threading.Thread(target=zoho_client.get_access_token) for _ in range(8)]
            [t.start() for t in threads]
            [t.join(10) for t in threads]
        self.assertEqual(len(calls), 1)
        self.assertEqual(zoho_client.get_access_token(), "t")
        zoho_client._access_token, zoho_client._access_token_expires_at = None, 0.0

    def test_token_refresh_is_not_rate_gated(self):
        zoho_client._access_token, zoho_client._access_token_expires_at = None, 0.0
        env = {"ZOHO_CLIENT_ID": "i", "ZOHO_CLIENT_SECRET": "s", "ZOHO_REFRESH_TOKEN": "r"}
        post = lambda url, data=None, timeout=None: SimpleNamespace(status_code=200, json=lambda: {"access_token": "t", "expires_in": 3600})
        with patch.dict(os.environ, env), patch.object(zoho_client.httpx, "post", post),                 patch.object(ZohoRateLimiter, "admit", side_effect=AssertionError("Accounts call must not use the Inventory limiter")):
            self.assertEqual(zoho_client.get_access_token(), "t")
        self.assertEqual(metrics.snapshot()["http_attempts"], 0)
        zoho_client._access_token, zoho_client._access_token_expires_at = None, 0.0


class SchedulerTests(Base):
    def test_scheduled_fleet_sync_still_runs_through_shared_limiter(self):
        fake = self.install(ScriptedZoho())
        order = SimpleNamespace(id="1", salesorder_number="SO-1", raw_json={"salesorder_id": "1"}, assignment_status="assigned",
                                vehicle_id=1, synced_at=None, completed_at=None)
        session = MagicMock()
        session.__enter__.return_value = session
        session.execute.return_value.scalars.return_value.all.return_value = []
        merged = []
        with patch.object(fleet, "SessionLocal", return_value=session), \
                patch.object(live_sales_order_cache, "refresh_shared_window", return_value=1), \
                patch.object(live_sales_order_cache, "get_assigned_snapshot", return_value=[order]), \
                patch.object(live_sales_order_cache, "merge_zoho_payload", side_effect=lambda o, f: merged.append(o.id)), \
                patch.object(live_sales_order_cache, "set_assignment"), \
                patch.object(fleet, "sync_history_row") as history:
            fleet.scheduled_fleet_sync()
        self.assertEqual(merged, ["1"])
        history.assert_called_once()
        self.assertEqual(fake.gets("salesorders/1"), 0)
        self.assertEqual(metrics.snapshot()["http_attempts"], 0)  # per-SO scheduler traffic removed


class LoggingTests(Base):
    def test_attempt_logs_include_waits_and_no_secrets(self):
        self.install(ScriptedZoho(script={0: (429, {"Retry-After": "1"})}))
        with self.assertLogs("zoho", level="INFO") as logs:
            zoho_client.fetch_sales_order_detail("1")
        text = "\n".join(logs.output)
        for needle in ("event=http_attempt", "rate_wait_ms=", "concurrency_wait_ms=", "event=http_429", "event=http_retry",
                       "retry_after=1", "attempt=2", "retry=1"):
            self.assertIn(needle, text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("Zoho-oauthtoken", text)

    def test_metrics_snapshot_fields(self):
        self.install(ScriptedZoho())
        zoho_client.fetch_sales_order_detail("1")
        snap = metrics.snapshot()
        for key in ("logical_requests", "http_attempts", "successful_attempts", "http_429", "retry_attempts", "coalesced_waiters",
                    "cache_hits", "cache_misses", "avg_rate_wait_ms", "avg_concurrency_wait_ms", "max_concurrency"):
            self.assertIn(key, snap)


if __name__ == "__main__":
    unittest.main()
