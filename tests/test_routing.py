import unittest

from services.ors_client import OrsError, _normalize_mapbox_feature, _validate_matrix
from services.route_costs import build_objective_cost_matrix, compute_route_cost_breakdown


class RoutingContractTests(unittest.TestCase):
    def test_mapbox_searchbox_poi_feature_is_normalized(self):
        feature = {
            "id": "poi.123",
            "geometry": {"type": "Point", "coordinates": [121.0, 14.6]},
            "properties": {
                "name": "Rare Global Food Trading Corp.",
                "full_address": "Unit SF02 Santana Grove, Parañaque City, Philippines",
                "feature_type": "poi",
            },
        }
        result = _normalize_mapbox_feature(feature, provider="mapbox-searchbox", default_label="fallback")
        self.assertEqual(result["label"], "Unit SF02 Santana Grove, Parañaque City, Philippines")
        self.assertEqual(result["type"], "poi")
        self.assertEqual(result["provider"], "mapbox-searchbox")
        self.assertEqual(result["lat"], 14.6)
        self.assertEqual(result["lng"], 121.0)

    def test_mapbox_geocoding_feature_falls_back_to_text_label(self):
        feature = {
            "geometry": {"type": "Point", "coordinates": [120.9, 14.5]},
            "place_type": ["address"],
            "text": "Baclaran",
        }
        result = _normalize_mapbox_feature(feature, provider="mapbox", default_label="fallback")
        self.assertEqual(result["label"], "Baclaran")
        self.assertEqual(result["type"], "address")

    def test_matrix_is_converted_to_km_and_minutes_with_metadata(self):
        result = _validate_matrix(
            {"distances": [[0, 12500], [12500, 0]], "durations": [[0, 1800], [1800, 0]]},
            "mapbox",
        )
        self.assertEqual(result["distance_matrix_km"][0][1], 12.5)
        self.assertEqual(result["duration_matrix_min"][0][1], 30.0)
        self.assertTrue(result["traffic_aware"])
        self.assertIsNotNone(result["calculated_at"])

    def test_unreachable_matrix_edge_is_rejected_instead_of_becoming_zero(self):
        with self.assertRaises(OrsError):
            _validate_matrix(
                {"distances": [[0, None], [1000, 0]], "durations": [[0, None], [60, 0]]},
                "mapbox",
            )

    def test_route_cost_breakdown_is_additive(self):
        breakdown = compute_route_cost_breakdown(
            10.0,
            60.0,
            distance_rate_per_km=20.0,
            time_rate_per_hour=30.0,
            fuel_surcharge_per_km=4.0,
            refrigeration_cost_per_hour=5.0,
            fixed_cost_per_route=6.0,
        )
        self.assertEqual(breakdown["distance"], 200.0)
        self.assertEqual(breakdown["time"], 30.0)
        self.assertEqual(breakdown["fuel"], 40.0)
        self.assertEqual(breakdown["refrigeration"], 5.0)
        self.assertEqual(breakdown["fixed"], 6.0)
        self.assertEqual(breakdown["total"], 281.0)

    def test_cheapest_objective_uses_configured_cost_rates(self):
        matrix = build_objective_cost_matrix(
            [[0.0, 5.0], [5.0, 0.0]],
            [[0.0, 30.0], [30.0, 0.0]],
            objective="cheapest",
            distance_rate_per_km=10.0,
            time_rate_per_hour=60.0,
            fuel_surcharge_per_km=2.0,
            refrigeration_cost_per_hour=3.0,
        )
        self.assertEqual(matrix[0][1], 91.5)


if __name__ == "__main__":
    unittest.main()
