import os
import tempfile
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

    def test_inventory_summary_uses_zoho_stock_for_each_live_order_sku(self):
        row = SimpleNamespace(
            id="so-multi",
            salesorder_number="SO26-18380",
            reference_number=None,
            customer_name="Customer",
            order_status="confirmed",
            invoice_status=None,
            payment_status=None,
            shipment_status=None,
            order_date=None,
            expected_shipment_date=date(2026, 10, 13),
            total=100,
            delivery_method=None,
            salesperson_name=None,
            synced_at=datetime(2026, 10, 12, tzinfo=ZoneInfo("Asia/Manila")),
            assignment_status="unassigned",
            vehicle_id=None,
            driver_id=None,
            assigned_at=None,
            assigned_by=None,
            assignment_batch_id=None,
            email_status=None,
            email_error=None,
            email_sent_at=None,
            shipping_address=None,
            raw_json={
                "branch_id": "4489499000001322444",
                "line_items": [
                    {"line_item_id": "4489499000272563433", "item_id": "4489499000074842271", "name": "Non-Food Styro Fishpac Body w/ Cover", "sku": "RGF-NF-0238", "quantity": 9, "unit": "pc"},
                    {"line_item_id": "4489499000272563434", "item_id": "4489499000071561700", "name": "BEEF CHUCK ROLL MONDELLI SLICED 2.5MM 1KG", "sku": "RGF-B401-1", "quantity": 110, "unit": "kg"},
                    {"line_item_id": "4489499000272563435", "item_id": "4489499000071039493", "name": "CHICKEN LEG FILLET SKIN ON SEARA", "sku": "RGF-C024", "quantity": 60, "unit": "kg"},
                ],
            },
        )
        stocks = {
            "4489499000074842271": {"mets": 177.0, "glacier": 226.0},
            "4489499000071561700": {"mets": 680.23, "glacier": 6.0},
            "4489499000071039493": {"mets": -552.0, "glacier": 34.0},
            "4489499000071564775": {"mets": 196.48, "glacier": 0},
            "4489499000239895407": {"mets": 5.38, "glacier": 0},
        }
        row_18441 = SimpleNamespace(**vars(row))
        row_18441.id = "so-18441"
        row_18441.salesorder_number = "SO26-18441"
        row_18441.raw_json = {
            "branch_id": "4489499000001322444",
            "line_items": [
                {"line_item_id": "li-p059", "item_id": "4489499000071564775", "name": "PORK ITEM A", "sku": "RGF-P059-2", "quantity": 1, "unit": "kg"},
                {"line_item_id": "li-p320", "item_id": "4489499000239895407", "name": "PORK ITEM B", "sku": "RGF-P320", "quantity": 1, "unit": "kg"},
            ],
        }
        with patch.object(load_planning, "row_to_dict", side_effect=lambda obj: vars(obj).copy()), \
                patch.object(load_planning, "cached_item_stock", side_effect=lambda item_id, branch_id=None: stocks[item_id]), \
                patch.object(load_planning, "calculate_line_weight_kg", return_value=None):
            summary = load_planning._summary(row, db=None, allow_fetch=False)
            summary_18441 = load_planning._summary(row_18441, db=None, allow_fetch=False)

        self.assertEqual([p["name"] for p in summary["products"]], ["Non-Food Styro Fishpac Body w/ Cover", "BEEF CHUCK ROLL MONDELLI SLICED 2.5MM 1KG", "CHICKEN LEG FILLET SKIN ON SEARA"])
        self.assertEqual([p["sku"] for p in summary["products"]], ["RGF-NF-0238", "RGF-B401-1", "RGF-C024"])
        self.assertEqual([(p["mets_qty_available_for_sale"], p["glacier_qty_available_for_sale"]) for p in summary["products"]], [(177.0, 226.0), (680.23, 6.0), (-552.0, 34.0)])
        self.assertEqual([(p["sku"], p["mets_qty_available_for_sale"], p["glacier_qty_available_for_sale"]) for p in summary_18441["products"]], [("RGF-P059-2", 196.48, 0), ("RGF-P320", 5.38, 0)])
        self.assertIsNone(summary_18441["mets_qty_available_for_sale"])
        self.assertIsNone(summary_18441["glacier_qty_available_for_sale"])

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
            # Stock-bearing batch results do not need the detail fallback. An
            # empty warehouses fixture unintentionally triggered live OAuth.
            return {"items": [{"item_id": item_id, "warehouses": [{"warehouse_id": "test", "warehouse_available_stock": 1}]} for item_id in item_ids]}

        with patch.object(item_detail_cache, "fetch_item_details_batch", side_effect=fake_batch), \
                patch.object(item_detail_cache, "_pool", InlinePool()), \
                patch.object(item_detail_cache, "fetch_item_detail") as detail:
            waiting = item_detail_cache.request_refresh(["A", "B", "A"])
            detail.assert_not_called()
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
                zoho_webhooks.flush_pending_webhooks()
        self.assertEqual(ok.json()["updated"], True)
        self.assertEqual(dup.json()["duplicate"], True)
        apply.assert_called_once()

    def test_feature_counters_and_budget_threshold(self):
        def fake_request(method, url, headers=None, params=None, timeout=None):
            path = url.split("/inventory/v1/")[1]
            if path == "salesorders":
                return SimpleNamespace(status_code=200, headers={}, json=lambda: {"code": 0, "salesorders": [], "page_context": {"has_more_page": False}})
            return SimpleNamespace(status_code=200, headers={}, json=lambda: {"code": 0, "salesorder": {"salesorder_id": "1"}})

        # Explicit guard setting: the default 92% rounds a 3-call budget to 3,
        # so it cannot test a cache-only stop after 2 calls.
        with patch.dict(os.environ, {"ZOHO_DAILY_BUDGET": "3", "ZOHO_GUARD_CACHE_ONLY_PCT": "66"}), \
                patch.object(zoho_client, "get_access_token", return_value="token"), \
                patch.object(zoho_client.httpx, "request", side_effect=fake_request):
            zoho_client.fetch_sales_order_detail("1")
            zoho_client.fetch_sales_orders(page=1, per_page=200)
            with self.assertRaises(zoho_client.ZohoError):
                zoho_client.fetch_sales_orders(page=1, per_page=200)
            snap = zoho_usage.snapshot()
        self.assertEqual(snap["by_feature"]["so_detail"], 1)
        self.assertEqual(snap["by_feature"]["inventory_list"], 1)
        self.assertEqual(snap["guard"], "cache_only")


if __name__ == "__main__":
    unittest.main()
