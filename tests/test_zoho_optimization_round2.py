import os
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("ZOHO_ORG_ID", "org-test")
os.environ.setdefault("ZOHO_WEBHOOK_SECRET", "secret")
os.environ.setdefault("JWT_SECRET", "test-secret")

from routers import load_planning, zoho_webhooks
from services import item_detail_cache, live_sales_order_cache as lc, zoho_client, zoho_rate_limiter, zoho_usage


def _row(order_id: str, ship="2026-10-09", modified="2026-10-08T01:00:00+00:00"):
    return {
        "salesorder_id": order_id,
        "salesorder_number": f"SO-{order_id}",
        "customer_name": "Customer",
        "status": "confirmed",
        "shipment_date": ship,
        "expected_shipment_date": ship,
        "last_modified_time": modified,
        "line_items": [],
    }


def _detail(order_id: str, modified="2026-10-08T01:00:00+00:00"):
    return {
        "salesorder": {
            **_row(order_id, modified=modified),
            "line_items": [
                {
                    "line_item_id": f"li-{order_id}",
                    "item_id": f"item-{order_id}",
                    "name": "Frozen Chicken",
                    "sku": f"SKU-{order_id}",
                    "quantity": 2,
                    "unit": "pack",
                    "package_details": {"weight": 1.5, "weight_unit": "kg"},
                }
            ],
        }
    }


class Round2OptimizationTests(unittest.TestCase):
    def setUp(self):
        zoho_usage.reset()
        zoho_rate_limiter.reset(rate_per_minute=600000.0, safety_percent=100.0, burst=1000, max_concurrency=100)
        lc.invalidate_windows()
        lc.invalidate_assigned_zoho()
        lc._wide_window_last_sync = None
        lc._wide_window_last_full = 0.0
        with lc._detail_lock:
            lc._details.clear()
        item_detail_cache.invalidate()
        self.addCleanup(zoho_rate_limiter.reset)

    def test_date_changes_inside_shared_window_make_no_zoho_call_after_warm(self):
        calls = []

        def fake_window(start, end, page=1, per_page=200, sort_column=None):
            calls.append((start, end, page, sort_column))
            return {"salesorders": [_row("1", "2026-10-09"), _row("2", "2026-10-15")], "page_context": {"has_more_page": False, "search_criteria": [{"column_name": "shipment_date"}]}}

        with patch.object(lc, "_wide_bounds", return_value=(date(2026, 10, 8), date(2026, 10, 22))), \
                patch.object(lc, "fetch_sales_orders_by_shipment_date", side_effect=fake_window), \
                patch.object(lc, "fetch_sales_order_detail", side_effect=lambda oid: _detail(oid, modified="2026-10-08T01:00:00+00:00") if oid == "1" else {"salesorder": {**_detail(oid)["salesorder"], "shipment_date": "2026-10-15", "expected_shipment_date": "2026-10-15"}}):
            self.assertEqual(len(lc.get_window(date(2026, 10, 9), date(2026, 10, 9))), 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(lc.get_window(date(2026, 10, 15), date(2026, 10, 15))), 1)
            self.assertEqual(len(calls), 1)

    def test_detail_hydration_only_for_new_or_changed_sales_orders(self):
        detail_calls = []
        modified = "2026-10-08T01:00:00+00:00"

        def fake_window(start, end, page=1, per_page=200, sort_column=None):
            return {"salesorders": [_row("1", modified=modified)], "page_context": {"has_more_page": False, "search_criteria": [{"column_name": "shipment_date"}]}}

        def fake_detail(order_id):
            detail_calls.append(order_id)
            return _detail(order_id, modified=modified)

        with patch.object(lc, "_wide_bounds", return_value=(date(2026, 10, 8), date(2026, 10, 22))), \
                patch.object(lc, "fetch_sales_orders_by_shipment_date", side_effect=fake_window), \
                patch.object(lc, "fetch_sales_order_detail", side_effect=fake_detail):
            rows = lc.get_window(date(2026, 10, 9), date(2026, 10, 9))
            self.assertEqual(detail_calls, ["1"])
            self.assertEqual(rows[0].raw_json["line_items"][0]["sku"], "SKU-1")

            lc.invalidate_windows()
            rows = lc.get_window(date(2026, 10, 9), date(2026, 10, 9))
            self.assertEqual(detail_calls, ["1"])
            self.assertEqual(rows[0].raw_json["line_items"][0]["name"], "Frozen Chicken")

            modified = "2026-10-08T02:00:00+00:00"
            lc.invalidate_windows()
            lc.get_window(date(2026, 10, 9), date(2026, 10, 9))
            self.assertEqual(detail_calls, ["1", "1"])

    def test_inventory_row_keeps_products_and_weight_from_hydrated_detail(self):
        def fake_window(start, end, page=1, per_page=200, sort_column=None):
            return {"salesorders": [_row("1")], "page_context": {"has_more_page": False, "search_criteria": [{"column_name": "shipment_date"}]}}

        with patch.object(lc, "_wide_bounds", return_value=(date(2026, 10, 8), date(2026, 10, 22))), \
                patch.object(lc, "fetch_sales_orders_by_shipment_date", side_effect=fake_window), \
                patch.object(lc, "fetch_sales_order_detail", side_effect=lambda oid: _detail(oid)):
            row = lc.get_window(date(2026, 10, 9), date(2026, 10, 9))[0]

        summary = load_planning._summary(row, db=None, allow_fetch=False)
        self.assertEqual(summary["products"][0]["name"], "Frozen Chicken")
        self.assertEqual(summary["products"][0]["sku"], "SKU-1")
        self.assertEqual(summary["products"][0]["total_weight_kg"], 3.0)

    def test_delta_sync_stops_when_page_is_older_than_last_sync(self):
        calls = []

        def fake_window(start, end, page=1, per_page=200, sort_column=None):
            calls.append((page, sort_column))
            rows = [_row("new", modified="2026-10-08T03:00:00+00:00")] if page == 1 else [_row("old", modified="2026-10-08T00:00:00+00:00")]
            return {"salesorders": rows, "raw_count": 200 if page == 1 else 1, "page_context": {"has_more_page": page == 1, "search_criteria": [{"column_name": "shipment_date"}]}}

        with patch.object(lc, "_wide_bounds", return_value=(date(2026, 10, 8), date(2026, 10, 22))), \
                patch.object(lc, "fetch_sales_orders_by_shipment_date", side_effect=fake_window):
            rows = lc._pull_delta_since(date(2026, 10, 8), date(2026, 10, 22), datetime.fromisoformat("2026-10-08T01:00:00+00:00"))
        self.assertEqual(set(rows), {"new"})
        self.assertEqual(calls, [(1, "last_modified_time"), (2, "last_modified_time")])

    def test_batch_stock_call_used_for_missing_items(self):
        seen = []

        class InlinePool:
            def submit(self, fn, *args, **kwargs):
                fn(*args, **kwargs)
                return None

        def fake_batch(item_ids):
            seen.append(list(item_ids))
            return {"items": [{"item_id": item_id, "warehouses": []} for item_id in item_ids]}

        with patch.object(item_detail_cache, "fetch_item_details_batch", side_effect=fake_batch), \
                patch.object(item_detail_cache, "_pool", InlinePool()):
            waiting = item_detail_cache.request_refresh(["A", "B", "A"])
        self.assertEqual(waiting, 2)
        self.assertEqual(seen, [["A", "B"]])

    def test_night_background_nonessential_call_is_blocked_before_http(self):
        zoho_usage.set_time_override(datetime(2026, 10, 8, 22, 0, tzinfo=ZoneInfo("Asia/Manila")))
        self.addCleanup(lambda: zoho_usage.set_time_override(None))
        with patch.object(zoho_client, "get_access_token", return_value="token"), \
                patch.object(zoho_client.httpx, "request") as http:
            with self.assertRaises(zoho_client.ZohoError):
                zoho_client.fetch_sales_orders(page=1, per_page=200)
        http.assert_not_called()

    def test_webhook_updates_cache_rejects_bad_secret_and_is_idempotent(self):
        app = FastAPI()
        app.include_router(zoho_webhooks.router)
        client = TestClient(app)
        with patch.dict(os.environ, {"ZOHO_WEBHOOKS_ENABLED": "true", "ZOHO_WEBHOOK_SECRET": "secret"}):
            bad = client.post("/api/zoho/webhooks/salesorder", headers={"X-Zoho-Webhook-Secret": "bad"}, json={"salesorder_id": "1"})
            self.assertEqual(bad.status_code, 401)
            with patch.object(lc, "apply_salesorder_webhook", return_value=True) as apply:
                body = {"event_id": "evt-1", "salesorder": {"salesorder_id": "1", "status": "confirmed", "last_modified_time": "t1"}}
                ok = client.post("/api/zoho/webhooks/salesorder", headers={"X-Zoho-Webhook-Secret": "secret"}, json=body)
                dup = client.post("/api/zoho/webhooks/salesorder", headers={"X-Zoho-Webhook-Secret": "secret"}, json=body)
        self.assertEqual(ok.json()["updated"], True)
        self.assertEqual(dup.json()["duplicate"], True)
        apply.assert_called_once()

    def test_feature_counters_and_budget_threshold(self):
        def fake_request(method, url, headers=None, params=None, timeout=None):
            path = url.split("/inventory/v1/")[1]
            if path == "salesorders":
                return SimpleNamespace(status_code=200, headers={}, json=lambda: {"code": 0, "salesorders": [], "page_context": {"has_more_page": False}})
            return SimpleNamespace(status_code=200, headers={}, json=lambda: {"code": 0, "salesorder": {"salesorder_id": "1"}})

        with patch.dict(os.environ, {"ZOHO_DAILY_BUDGET": "3"}), \
                patch.object(zoho_client, "get_access_token", return_value="token"), \
                patch.object(zoho_client.httpx, "request", side_effect=fake_request):
            zoho_client.fetch_sales_order_detail("1")
            zoho_client.fetch_sales_orders(page=1, per_page=200)
            with self.assertRaises(zoho_client.ZohoError):
                zoho_client.fetch_sales_orders(page=1, per_page=200)
            snap = zoho_usage.snapshot()
        self.assertEqual(snap["by_feature"]["so_detail"], 1)
        self.assertEqual(snap["by_feature"]["inventory_list"], 1)
        self.assertEqual(snap["guard"], "warn")


if __name__ == "__main__":
    unittest.main()
