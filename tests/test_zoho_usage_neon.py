"""Zoho usage counter: Neon-backed, additive, restart-safe. Mocked only - no live Zoho calls."""
from __future__ import annotations

import os
import re
import sys
import unittest
from collections import Counter
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost")

from services import zoho_client, zoho_so_lock, zoho_usage  # noqa: E402


class FakeStore:
    """Stands in for Neon: additive writes, loadable by day."""

    def __init__(self):
        self.rows: Counter = Counter()
        self.down = False
        self.write_calls = 0

    def load(self, day):
        if self.down:
            raise ConnectionError("neon down")
        return {(c, s): v for (d, c, s), v in self.rows.items() if d == day}

    def write(self, rows):
        self.write_calls += 1
        if self.down:
            raise ConnectionError("neon down")
        for day, cat, src, count in rows:
            self.rows[(day, cat, src)] += count

    def total(self, counted_only=True):
        return sum(v for (d, c, s), v in self.rows.items() if not (counted_only and c in zoho_usage.UNCOUNTED))


class UsageCase(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        zoho_usage.set_store(self.store)
        zoho_usage.set_time_override(None)
        zoho_usage.reset()
        self.addCleanup(zoho_usage.set_store, None)
        self.addCleanup(zoho_usage.stop)
        self.addCleanup(zoho_usage.reset)

    def new_process(self):
        """Simulate a restart: all memory gone, Neon rows survive."""
        zoho_usage.stop()
        zoho_usage.reset(restored=False)
        return zoho_usage.start("test-instance")


class RestartTests(UsageCase):
    def test_calls_flush_restart_restore(self):
        for _ in range(7):
            zoho_usage.record_call("so_detail")
        zoho_usage.record_call("item_detail", background=True)
        zoho_usage.record_call("token_refresh")
        self.assertTrue(zoho_usage.flush())
        self.assertEqual(self.store.total(), 8)
        snap = self.new_process()
        self.assertTrue(snap["usage_restored_from_db"]["restored"])
        self.assertEqual(snap["usage_restored_from_db"]["total"], 8)
        self.assertEqual(snap["zoho_calls_today"], 8)
        self.assertEqual(snap["token_refresh"], 1)
        self.assertEqual(snap["by_source"], {"request": 7, "background": 1})
        with patch.dict(os.environ, {"ZOHO_DAILY_BUDGET": "10"}):  # 8 of 10 used -> 80% guard survives the restart
            with self.assertRaises(zoho_usage.ZohoBudgetGuard):
                zoho_usage.enforce_budget("inventory_list")
            zoho_usage.enforce_budget("so_detail")  # essential still allowed
        zoho_usage.record_call("so_detail")
        self.assertEqual(zoho_usage.snapshot()["zoho_calls_today"], 9)  # monotonic after restart

    def test_second_flush_is_additive_not_absolute(self):
        zoho_usage.record_call("so_detail")
        zoho_usage.flush()
        zoho_usage.record_call("so_detail")
        zoho_usage.record_call("so_detail")
        zoho_usage.flush()
        self.assertEqual(self.store.total(), 3)

    def test_idle_means_zero_writes(self):
        zoho_usage.flush()
        zoho_usage.flush()
        self.assertEqual(self.store.write_calls, 0)

    def test_shutdown_stop_flushes_and_logs(self):
        for _ in range(3):
            zoho_usage.record_call("so_detail")
        with self.assertLogs("zoho_usage", level="INFO") as logs:
            zoho_usage.stop()
        self.assertEqual(self.store.total(), 3)
        self.assertIn("zoho_usage shutdown flush: 3 calls persisted", " ".join(logs.output))

    def test_no_custom_signal_handler_is_installed(self):
        import signal
        before = signal.getsignal(signal.SIGTERM)
        zoho_usage.start("x")
        zoho_usage.stop()
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        self.assertFalse(hasattr(zoho_usage, "install_sigterm_flush"))

    def test_deploy_overlap_rereads_total_after_flush(self):
        today = date.fromisoformat(zoho_usage.snapshot()["date"])
        self.store.rows[(today, "so_detail", "request")] = 100
        self.assertEqual(self.new_process()["zoho_calls_today"], 100)       # instance A restores 100
        zoho_usage.record_call("so_detail")
        zoho_usage.record_call("so_detail")                                  # A's own delta: 2
        self.store.rows[(today, "so_detail", "request")] += 20              # old instance B flushes 20 more
        self.assertEqual(zoho_usage.snapshot()["zoho_calls_today"], 102)    # not yet visible to A
        self.assertTrue(zoho_usage.flush())                                  # A's next cycle: write 2, read back 122
        snap = zoho_usage.snapshot()
        self.assertEqual(snap["zoho_calls_today"], 122)                     # 100 + B's 20 + A's 2
        zoho_usage.record_call("so_detail")
        self.assertEqual(zoho_usage.snapshot()["zoho_calls_today"], 123)    # unflushed delta stays on top
        with patch.dict(os.environ, {"ZOHO_DAILY_BUDGET": "150"}):          # guard uses the refreshed total (123 >= 80%)
            with self.assertRaises(zoho_usage.ZohoBudgetGuard):
                zoho_usage.enforce_budget("inventory_list")

    def test_health_never_reads_neon_and_admin_endpoint_throttles_to_one_read_per_five_minutes(self):
        today = date.fromisoformat(zoho_usage.snapshot()["date"])
        self.store.rows[(today, "so_detail", "request")] = 10
        self.new_process()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from auth.dependencies import get_current_user
        from routers import admin as admin_router
        import main
        reads = []
        original = self.store.load
        self.store.load = lambda day: (reads.append(day), original(day))[1]
        self.store.rows[(today, "so_detail", "request")] += 5            # another instance writes while we are idle
        app = FastAPI()
        app.include_router(admin_router.usage_router)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, email="a@x.com", role="admin", full_name="A", status="active")
        client = TestClient(app)
        stale = lambda: patch.object(zoho_usage.time, "monotonic", return_value=zoho_usage._last_readback + 301)
        self.addCleanup(main.schema_state.update, dict(main.schema_state))
        main.schema_state.update(ok=True, current="x", head="x")
        with stale():                                                      # even when the baseline is stale, /health is memory-only
            for _ in range(3):
                body = main.health()
        self.assertEqual(reads, [])
        self.assertEqual(body["zoho_calls_today"], 10)
        self.assertIn("process_started_at", body)
        self.assertIn("restored", body["usage_restored_from_db"])
        r = client.get("/api/admin/zoho-usage")                           # fresh baseline: no read
        self.assertEqual((r.status_code, reads, r.json()["zoho_calls_today"]), (200, [], 10))
        with stale():
            first = client.get("/api/admin/zoho-usage").json()            # stale: exactly one read
            client.get("/api/admin/zoho-usage")                           # inside the window: none
        self.assertEqual(len(reads), 1)
        self.assertEqual(first["zoho_calls_today"], 15)
        self.assertEqual(first["zoho_usage"]["org_limit"], 10000)
        non_admin = FastAPI()
        non_admin.include_router(admin_router.usage_router)
        non_admin.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2, email="d@x.com", role="dispatcher", full_name="D", status="active")
        self.assertEqual(TestClient(non_admin).get("/api/admin/zoho-usage").status_code, 403)

    def test_readback_failure_keeps_local_baseline(self):
        zoho_usage.record_call("so_detail")
        original_load = self.store.load
        self.store.load = lambda day: (_ for _ in ()).throw(ConnectionError("down"))
        self.assertTrue(zoho_usage.flush())
        self.store.load = original_load
        self.assertEqual(zoho_usage.snapshot()["zoho_calls_today"], 1)
        self.assertEqual(zoho_usage.snapshot()["unflushed"], 0)

    def test_midnight_calls_bucket_by_call_day_and_guard_resets(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        manila = ZoneInfo("Asia/Manila")
        with patch.dict(os.environ, {"ZOHO_DAILY_BUDGET": "10"}):
            zoho_usage.set_time_override(datetime(2026, 10, 8, 23, 59, 50, tzinfo=manila))
            for _ in range(9):
                zoho_usage.record_call("inventory_list")
            with self.assertRaises(zoho_usage.ZohoBudgetGuard):
                zoho_usage.enforce_budget("inventory_list")                 # 9/10 on the old day: blocked
            zoho_usage.set_time_override(datetime(2026, 10, 9, 0, 0, 20, tzinfo=manila))
            zoho_usage.enforce_budget("inventory_list")                     # new Manila day: full budget again
            zoho_usage.record_call("inventory_list")
            self.assertTrue(zoho_usage.flush())                             # one flush, two usage_day rows
            self.assertEqual(self.store.rows[(date(2026, 10, 8), "inventory_list", "request")], 9)
            self.assertEqual(self.store.rows[(date(2026, 10, 9), "inventory_list", "request")], 1)
            snap = zoho_usage.snapshot()
            self.assertEqual(snap["date"], "2026-10-09")
            self.assertEqual(snap["zoho_calls_today"], 1)
            self.assertEqual(snap["guard"], "ok")
            for _ in range(7):
                zoho_usage.enforce_budget("inventory_list")
                zoho_usage.record_call("inventory_list")                    # 8 of 10 allowed before the 80% block
            with self.assertRaises(zoho_usage.ZohoBudgetGuard):
                zoho_usage.enforce_budget("inventory_list")

    def test_failsafe_scope_while_restore_failed_then_normal_rules(self):
        self.store.down = True
        with patch.object(zoho_usage, "RESTORE_RETRY_SECONDS", 3600), patch.object(zoho_usage, "FLUSH_SECONDS", 3600),                 patch.dict(os.environ, {"ZOHO_FAILSAFE_SO_DETAIL_CAP": "3"}):
            zoho_usage.start("x")
            for feature in ("acknowledge", "lock_status"):
                for _ in range(10):
                    zoho_usage.enforce_budget(feature)                      # free
                    zoho_usage.record_call(feature)
            for _ in range(3):
                zoho_usage.enforce_budget("so_detail")                      # under the cap
                zoho_usage.record_call("so_detail")
            with self.assertRaises(zoho_usage.ZohoBudgetGuard):
                zoho_usage.enforce_budget("so_detail")                      # cap reached
            with self.assertRaises(zoho_usage.ZohoBudgetGuard):
                zoho_usage.enforce_budget("items_batch")                    # everything else blocked
            self.store.down = False
            self.assertTrue(zoho_usage.restore())
            for _ in range(5):
                zoho_usage.enforce_budget("so_detail")                      # normal rules: so_detail exempt from the budget
            zoho_usage.enforce_budget("items_batch")                        # normal rules: low usage, allowed

    def test_failsafe_default_cap_is_50(self):
        self.assertEqual(zoho_usage.failsafe_so_detail_cap(), 50)

    def test_neon_down_at_startup_fails_safe_then_recovers(self):
        self.store.rows[(date.fromisoformat(zoho_usage.snapshot()["date"]), "so_detail", "request")] = 3500
        self.store.down = True
        with patch.object(zoho_usage, "RESTORE_RETRY_SECONDS", 3600), patch.object(zoho_usage, "FLUSH_SECONDS", 3600):
            snap = zoho_usage.start("test-instance")
        self.assertFalse(snap["usage_restored_from_db"]["restored"])
        self.assertEqual(snap["guard"], "unknown_restoring")
        with self.assertRaises(zoho_usage.ZohoBudgetGuard):
            zoho_usage.enforce_budget("inventory_list")
        zoho_usage.enforce_budget("acknowledge")  # essential allowed
        zoho_usage.record_call("so_detail")  # counted in memory meanwhile, never dropped
        self.store.down = False
        self.assertTrue(zoho_usage.restore())
        snap = zoho_usage.snapshot()
        self.assertEqual(snap["zoho_calls_today"], 3501)  # restored 3500 + the 1 counted while down
        self.assertEqual(snap["guard"], "cache_only")
        with self.assertRaises(zoho_usage.ZohoBudgetGuard):
            zoho_usage.enforce_budget("inventory_list")  # 3501 >= 80% of 4000, restored value drives the guard

    def test_flush_failure_keeps_delta_and_retries(self):
        for _ in range(4):
            zoho_usage.record_call("so_detail")
        self.store.down = True
        self.assertFalse(zoho_usage.flush())
        self.assertEqual(zoho_usage.snapshot()["zoho_calls_today"], 4)
        self.assertEqual(zoho_usage.snapshot()["unflushed"], 4)
        zoho_usage.record_call("so_detail")
        self.store.down = False
        self.assertTrue(zoho_usage.flush())
        self.assertEqual(self.store.total(), 5)
        self.assertEqual(zoho_usage.snapshot()["unflushed"], 0)
        self.assertEqual(zoho_usage.snapshot()["zoho_calls_today"], 5)

    def test_total_is_monotonic_across_flush_and_new_sessions(self):
        seen = []
        for i in range(6):
            zoho_usage.record_call("so_detail")
            if i % 2:
                zoho_usage.flush()
            seen.append(zoho_usage.snapshot()["zoho_calls_today"])  # what /health shows on each login
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 6)

    def test_labelled_counts_as_fleet_sync_background(self):
        with zoho_usage.labelled("fleet_sync"):
            zoho_usage.record_call("so_detail")
        snap = zoho_usage.snapshot()
        self.assertEqual(snap["by_feature"]["fleet_sync"], 1)
        self.assertEqual(snap["by_source"]["background"], 1)
        self.assertEqual(snap["by_feature"]["other"], 0)  # "other" always visible

    def test_unknown_category_lands_in_other(self):
        zoho_usage.record_call("mystery")
        self.assertEqual(zoho_usage.snapshot()["by_feature"]["other"], 1)

    def test_day_rollover_flushes_old_day_under_its_own_date(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        zoho_usage.set_time_override(datetime(2026, 10, 8, 23, 59, tzinfo=ZoneInfo("Asia/Manila")))
        zoho_usage.record_call("so_detail")
        zoho_usage.set_time_override(datetime(2026, 10, 9, 0, 1, tzinfo=ZoneInfo("Asia/Manila")))
        zoho_usage.record_call("so_detail")
        self.assertEqual(zoho_usage.snapshot()["zoho_calls_today"], 1)
        zoho_usage.flush()
        self.assertEqual(self.store.rows[(date(2026, 10, 8), "so_detail", "request")], 1)
        self.assertEqual(self.store.rows[(date(2026, 10, 9), "so_detail", "request")], 1)


class SqlUpsertTests(unittest.TestCase):
    """Runs the real UPSERT statement (SQLite dialect here, same ON CONFLICT DO UPDATE as PostgreSQL)."""

    def test_additive_upsert_and_load(self):
        engine = sa.create_engine("sqlite://")
        zoho_usage.usage_table.create(engine)
        store = zoho_usage.DbStore(sessionmaker(bind=engine))
        day = date(2026, 10, 9)
        store.write([(day, "so_detail", "request", 5), (day, "token_refresh", "request", 1)])
        store.write([(day, "so_detail", "request", 3)])  # a second worker / next flush adds, never overwrites
        self.assertEqual(store.load(day), {("so_detail", "request"): 8, ("token_refresh", "request"): 1})
        self.assertEqual(store.load(date(2026, 10, 10)), {})


class CallSiteCountingTests(UsageCase):
    ENV = {"ZOHO_ORG_ID": "org-test", "ZOHO_CLIENT_ID": "c", "ZOHO_CLIENT_SECRET": "s", "ZOHO_REFRESH_TOKEN": "r"}

    def test_429_retry_counts_both_attempts(self):
        responses = [SimpleNamespace(status_code=429, headers={"Retry-After": "0"}, json=lambda: {}),
                     SimpleNamespace(status_code=200, headers={}, json=lambda: {"code": 0, "salesorder": {"salesorder_id": "1"}})]
        with patch.dict(os.environ, self.ENV), patch.object(zoho_client, "get_access_token", return_value="t"), \
                patch.object(zoho_client, "_sleep", lambda s: None), \
                patch.object(zoho_client.httpx, "request", side_effect=lambda *a, **k: responses.pop(0)):
            zoho_client.fetch_sales_order_detail("1")
        self.assertEqual(zoho_usage.snapshot()["by_feature"]["so_detail"], 2)

    def test_401_attempt_is_counted_and_token_refresh_is_excluded_from_total(self):
        zoho_client._access_token = None
        token_resp = SimpleNamespace(status_code=200, json=lambda: {"access_token": "new", "expires_in": 3600})
        api_resp = SimpleNamespace(status_code=401, headers={}, json=lambda: {})
        self.addCleanup(setattr, zoho_client, "_access_token", None)
        with patch.dict(os.environ, self.ENV), patch.object(zoho_client.httpx, "post", return_value=token_resp), \
                patch.object(zoho_client.httpx, "request", return_value=api_resp):
            with self.assertRaises(zoho_client.ZohoError):
                zoho_client.fetch_sales_order_detail("1")
        snap = zoho_usage.snapshot()
        self.assertEqual(snap["by_feature"]["so_detail"], 1)   # the 401 attempt reached Zoho: counted
        self.assertEqual(snap["token_refresh"], 1)             # refresh tracked separately
        self.assertEqual(snap["zoho_calls_today"], 1)          # and excluded from the total

    def test_lock_credential_refresh_and_calls(self):
        zoho_so_lock.reset_token_cache()
        self.addCleanup(zoho_so_lock.reset_token_cache)
        env = {**self.ENV, "ZOHO_LOCK_CLIENT_ID": "c", "ZOHO_LOCK_CLIENT_SECRET": "s", "ZOHO_LOCK_REFRESH_TOKEN": "r"}
        token_resp = SimpleNamespace(status_code=200, json=lambda: {"access_token": "lock", "expires_in": 3600})
        api_resp = SimpleNamespace(status_code=200, json=lambda: {})
        with patch.dict(os.environ, env), patch.object(zoho_so_lock.httpx, "post", return_value=token_resp), \
                patch.object(zoho_so_lock.httpx, "request", return_value=api_resp):
            zoho_so_lock._request("GET", "https://x/y")
        snap = zoho_usage.snapshot()
        self.assertEqual(snap["by_feature"]["lock_status"], 1)
        self.assertEqual(snap["token_refresh"], 1)
        self.assertEqual(snap["zoho_calls_today"], 1)

    def test_items_are_split_into_batch_and_detail(self):
        ok = SimpleNamespace(status_code=200, headers={}, json=lambda: {"code": 0, "items": [], "item": {}})
        with patch.dict(os.environ, self.ENV), patch.object(zoho_client, "get_access_token", return_value="t"), \
                patch.object(zoho_client.httpx, "request", return_value=ok):
            zoho_client.fetch_item_details_batch(["A", "B"])
            zoho_client.fetch_item_detail("A")
        by = zoho_usage.snapshot()["by_feature"]
        self.assertEqual((by["items_batch"], by["item_detail"]), (1, 1))


class HostChokePointTest(unittest.TestCase):
    ALLOWED = {"zoho_client.py", "zoho_so_lock.py", "zoho_usage.py"}
    PATTERN = re.compile(r"zohoapis|inventory/v1|books/v3|accounts\.zoho")
    SKIP_DIRS = {"tests", "venv", "scratch", "__pycache__", "migrations", ".runtime", "node_modules"}

    def test_no_zoho_host_outside_the_choke_point(self):
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in root.rglob("*.py"):
            if self.SKIP_DIRS & set(path.relative_to(root).parts) or path.name in self.ALLOWED:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if self.PATTERN.search(line):
                    offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
