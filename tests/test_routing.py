import unittest
import os
from types import SimpleNamespace

os.environ.setdefault("JWT_SECRET", "test")

from routers.routes import _split_round_trip
from services.google_route_optimization import build_request
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

    def test_refrigeration_can_be_disabled_for_empty_return_leg(self):
        breakdown = compute_route_cost_breakdown(
            10.0,
            60.0,
            distance_rate_per_km=0.0,
            time_rate_per_hour=0.0,
            fuel_surcharge_per_km=0.0,
            refrigeration_cost_per_hour=5.0,
            include_refrigeration=False,
        )
        self.assertEqual(breakdown["refrigeration"], 0.0)
        self.assertEqual(breakdown["total"], 0.0)

    def test_round_trip_split_adds_return_cost_without_return_refrigeration_by_default(self):
        config = {
            "distance_rate_per_km": 10.0,
            "distance_rate_configured": True,
            "time_rate_per_hour": 60.0,
            "time_rate_configured": True,
            "fuel_surcharge_per_km": 2.0,
            "fuel_surcharge_configured": True,
            "refrigeration_cost_per_hour": 30.0,
            "refrigeration_cost_configured": True,
            "fixed_cost_per_route": 5.0,
            "fixed_cost_configured": True,
            "refrigeration_on_return_leg": False,
        }
        split = _split_round_trip([{"distance_km": 10, "duration_min": 60}, {"distance_km": 5, "duration_min": 30}], True, config)
        self.assertEqual(split["outbound"]["distanceKm"], 10.0)
        self.assertEqual(split["return"]["distanceKm"], 5.0)
        self.assertEqual(split["return"]["costBreakdown"]["refrigeration"], 0.0)
        self.assertEqual(split["total"]["distanceKm"], 15.0)
        self.assertEqual(split["total"]["costBreakdown"]["total"], 305.0)

    def test_round_trip_split_can_apply_return_refrigeration(self):
        config = {
            "distance_rate_per_km": 0.0, "distance_rate_configured": True,
            "time_rate_per_hour": 0.0, "time_rate_configured": True,
            "fuel_surcharge_per_km": 0.0, "fuel_surcharge_configured": True,
            "refrigeration_cost_per_hour": 30.0, "refrigeration_cost_configured": True,
            "fixed_cost_per_route": 0.0, "fixed_cost_configured": True,
            "refrigeration_on_return_leg": True,
        }
        split = _split_round_trip([{"distance_km": 1, "duration_min": 60}, {"distance_km": 1, "duration_min": 30}], True, config)
        self.assertEqual(split["return"]["costBreakdown"]["refrigeration"], 15.0)
        self.assertEqual(split["total"]["costBreakdown"]["refrigeration"], 45.0)

    def test_google_optimization_end_location_only_when_return_checked(self):
        vehicle = SimpleNamespace(id=1, start_lat=14.0, start_lng=121.0, end_lat=15.0, end_lng=122.0, has_end_location=True, capacity_kg=1000, cost_per_km=10, cost_per_hour=100, shift_start=0, shift_end=1440)
        shipment = SimpleNamespace(id=10, lat=14.5, lng=121.5, service_time_min=15, time_window_start=0, time_window_end=1440, demand_kg=1)
        request = build_request(SimpleNamespace(vehicles=[vehicle], shipments=[shipment]))
        self.assertIn("endLocation", request["model"]["vehicles"][0])
        vehicle.has_end_location = False
        request = build_request(SimpleNamespace(vehicles=[vehicle], shipments=[shipment]))
        self.assertNotIn("endLocation", request["model"]["vehicles"][0])

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
