import unittest
from unittest.mock import patch

from services.item_weight import calculate_line_weight_kg


class ItemWeightTests(unittest.TestCase):
    @patch("services.item_weight.fetch_item_detail")
    def test_kg_is_quantity_and_never_looks_up_item(self, fetch):
        self.assertEqual(calculate_line_weight_kg(30, "kg", "kg-item", item={"package_details": {"weight": 30, "weight_unit": "kg"}}), 30)
        fetch.assert_not_called()

    @patch("services.item_weight.fetch_item_detail")
    def test_kg_normalization_regression(self, fetch):
        for unit in ("kg", "KG", "Kg", " kg "):
            self.assertEqual(calculate_line_weight_kg(30, unit, "kg-regression"), 30)
        fetch.assert_not_called()

    @patch("services.item_weight.fetch_item_detail")
    def test_kg_line_total_is_not_multiplied_twice(self, fetch):
        quantity = 30
        line_total = calculate_line_weight_kg(quantity, "kg", "RGF-S035")
        self.assertEqual(line_total, 30)
        fetch.assert_not_called()

    @patch("services.item_weight.fetch_item_detail", return_value={"item": {"package_details": {"weight": 20, "weight_unit": "kg"}}})
    def test_case_uses_package_weight(self, _fetch):
        self.assertEqual(calculate_line_weight_kg(1, "case", "case-item", item={"item_id": "case-item", "sku": "RGF-SL253-6"}), 20)

    @patch("services.item_weight.fetch_item_detail", return_value={"item": {"package_details": {"weight": 20, "weight_unit": "kg"}}})
    def test_multiple_cases_use_zoho_case_weight(self, _fetch):
        self.assertEqual(calculate_line_weight_kg(3, "case", "case-multi", item={"item_id": "case-multi"}), 60)

    @patch("services.item_weight.fetch_item_detail", return_value={"item": {"package_details": {"weight": 0.013, "weight_unit": "kg"}}})
    def test_pc_decimal_weight(self, _fetch):
        self.assertAlmostEqual(calculate_line_weight_kg(5300, "pc", "pc-item"), 68.9)

    @patch("services.item_weight.fetch_item_detail", return_value={"item": {"package_details": {"weight": 0.013, "weight_unit": "kg"}}})
    def test_pc_line_without_embedded_package_details_fetches_item(self, _fetch):
        self.assertAlmostEqual(calculate_line_weight_kg(3, "pc", "pc-line", item={"item_id": "pc-line", "sku": "RGF-NF-0238"}), 0.039)

    @patch("services.item_weight.fetch_item_detail", return_value={"item": {"package_details": {"weight": 1000, "weight_unit": "g"}}})
    def test_grams(self, _fetch):
        self.assertEqual(calculate_line_weight_kg(1, "case", "gram-item"), 1)

    @patch("services.item_weight.fetch_item_detail", return_value={"item": {"package_details": {"weight": 10, "weight_unit": "lb"}}})
    def test_pounds(self, _fetch):
        self.assertAlmostEqual(calculate_line_weight_kg(1, "case", "lb-item"), 4.5359237)

    @patch("services.item_weight.fetch_item_detail", return_value={"item": {"package_details": {"weight": 10, "weight_unit": "oz"}}})
    def test_ounces(self, _fetch):
        self.assertAlmostEqual(calculate_line_weight_kg(1, "case", "oz-item"), 0.28349523125)

    @patch("services.item_weight.fetch_item_detail", return_value={"item": {"package_details": {}}})
    def test_missing_weight_is_unknown(self, _fetch):
        self.assertIsNone(calculate_line_weight_kg(1, "case", "missing-item"))


if __name__ == "__main__":
    unittest.main()
