import unittest

from services.delivery_status import is_delivered, sales_order_delivery_status


class DeliveryStatusTests(unittest.TestCase):
    def test_all_delivered_packages_are_delivered(self):
        self.assertEqual(sales_order_delivery_status({"packages": [{"status": "DELIVERED"}]}), "Delivered")
        self.assertTrue(is_delivered({"packages": [{"status": "DELIVERED"}]}))

    def test_closed_order_is_not_delivery_proof(self):
        self.assertEqual(sales_order_delivery_status({"status": "closed"}), "Unknown")
        self.assertFalse(is_delivered({"status": "closed"}))

    def test_partial_package_delivery_is_not_removed_from_active_load(self):
        raw = {"packages": [{"status": "DELIVERED"}, {"status": "SHIPPED"}]}
        self.assertEqual(sales_order_delivery_status(raw), "Partially delivered")
        self.assertFalse(is_delivered(raw))

    def test_refresh_calculation_is_idempotent(self):
        orders = [
            {"weight": 200, "raw": {"packages": [{"status": "PENDING"}]}},
            {"weight": 300, "raw": {"packages": [{"status": "DELIVERED"}]}},
        ]
        def active_load():
            return sum(o["weight"] for o in orders if not is_delivered(o["raw"]))
        self.assertEqual(active_load(), 200)
        self.assertEqual(active_load(), 200)


if __name__ == "__main__":
    unittest.main()
