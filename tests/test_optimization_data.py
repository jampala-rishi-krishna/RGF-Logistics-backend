import unittest

from services.optimization_data import normalize_weight_kg


class WeightNormalizationTests(unittest.TestCase):
    def test_kilograms_pass_through(self):
        self.assertEqual(normalize_weight_kg(620, "kg"), 620.0)

    def test_grams_converted_to_kilograms(self):
        self.assertEqual(normalize_weight_kg(620000, "g"), 620.0)

    def test_pounds_converted_to_kilograms(self):
        self.assertEqual(normalize_weight_kg(1000, "lbs"), round(1000 * 0.45359237, 3))

    def test_tonnes_converted_to_kilograms(self):
        self.assertEqual(normalize_weight_kg(1.5, "tonnes"), 1500.0)

    def test_unit_is_case_and_whitespace_insensitive(self):
        self.assertEqual(normalize_weight_kg(10, "  KG  "), 10.0)

    def test_missing_value_returns_none_not_a_default(self):
        self.assertIsNone(normalize_weight_kg(None, "kg"))

    def test_zero_or_negative_value_returns_none(self):
        self.assertIsNone(normalize_weight_kg(0, "kg"))
        self.assertIsNone(normalize_weight_kg(-5, "kg"))

    def test_missing_unit_returns_none_even_with_a_value(self):
        self.assertIsNone(normalize_weight_kg(500, None))

    def test_unrecognized_unit_returns_none_rather_than_guessing(self):
        self.assertIsNone(normalize_weight_kg(500, "stones"))


if __name__ == "__main__":
    unittest.main()
