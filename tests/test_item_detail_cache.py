import unittest
from unittest.mock import patch

from services import item_detail_cache


class ItemDetailCacheTests(unittest.TestCase):
    def tearDown(self):
        item_detail_cache.invalidate()

    @patch("services.item_detail_cache.fetch_item_details_batch")
    @patch("services.item_detail_cache.fetch_item_detail")
    def test_batch_without_warehouses_is_rehydrated_from_detail(self, fetch_detail, fetch_batch):
        fetch_batch.return_value = {
            "items": [
                {
                    "item_id": "i1",
                    "package_details": {"weight": 1, "weight_unit": "kg"},
                }
            ]
        }
        fetch_detail.return_value = {
            "item": {
                "package_details": {"weight": 1, "weight_unit": "kg"},
                "warehouses": [{"warehouse_name": "Mets Cold Storage Services Inc. RGF", "warehouse_available_for_sale_stock": 7}],
            }
        }

        item_detail_cache._batch_refresh_worker(["i1"])

        cached, fresh = item_detail_cache.get_cached("i1")
        self.assertTrue(fresh)
        self.assertEqual(cached["warehouses"][0]["warehouse_available_for_sale_stock"], 7)
        fetch_detail.assert_called_once_with("i1")

    @patch("services.item_detail_cache.fetch_item_details_batch")
    @patch("services.item_detail_cache.fetch_item_detail")
    def test_batch_with_warehouses_does_not_fetch_detail_again(self, fetch_detail, fetch_batch):
        fetch_batch.return_value = {
            "items": [
                {
                    "item_id": "i1",
                    "warehouses": [{"warehouse_name": "Glacier South RGF", "warehouse_available_for_sale_stock": 3}],
                }
            ]
        }

        item_detail_cache._batch_refresh_worker(["i1"])

        cached, fresh = item_detail_cache.get_cached("i1")
        self.assertTrue(fresh)
        self.assertEqual(cached["warehouses"][0]["warehouse_available_for_sale_stock"], 3)
        fetch_detail.assert_not_called()

    def test_fresh_cache_without_warehouses_is_still_pending_for_lists(self):
        with item_detail_cache._lock:
            item_detail_cache._items["i1"] = (999999999.0, {"package_details": {"weight": 1}, "warehouses": []})
            item_detail_cache._inflight.add("i1")

        try:
            self.assertEqual(item_detail_cache.request_refresh(["i1"]), 1)
        finally:
            with item_detail_cache._lock:
                item_detail_cache._inflight.discard("i1")


if __name__ == "__main__":
    unittest.main()
