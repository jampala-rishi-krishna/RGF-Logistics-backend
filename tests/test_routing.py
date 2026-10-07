import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("JWT_SECRET", "test")

from fastapi import HTTPException

from routers.routes import RoutePlanBody, _split_round_trip, plan_route
from services import optimizer
from services.google_maps import fetch_route_polyline
from services.google_route_optimization import build_request
from services.ors_client import OrsError, _normalize_mapbox_feature, _validate_matrix
from services.route_costs import build_objective_cost_matrix, build_route_cost_config, compute_route_cost_breakdown, describe_rates, load_route_cost_config
from services.warehouses import ReturnWarehouseRequired, get_warehouse, resolve_return_warehouse

ENV_RATES = {
    "ROUTE_DIESEL_PRICE_PER_LITER": "95", "ROUTE_FUEL_KM_PER_LITER": "7", "ROUTE_DRIVER_COST_PER_HOUR": "120",
    "ROUTE_HELPER_COST_PER_HOUR": "120", "ROUTE_DISTANCE_COST_PER_KM": "5",
    "ROUTE_REFRIGERATION_LITERS_PER_HOUR_CHILLED": "0.8", "ROUTE_REFRIGERATION_LITERS_PER_HOUR_FROZEN": "1.2",
    "ROUTE_REFRIGERATION_ON_RETURN_LEG": "false",
}


def make_config(**overrides):
    values = dict(distance_rate_per_km=5.0, diesel_price_per_liter=95.0, fuel_km_per_liter=7.0, driver_cost_per_hour=120.0,
                  helper_cost_per_hour=120.0, refrigeration_liters_per_hour_chilled=0.8, refrigeration_liters_per_hour_frozen=1.2,
                  refrigeration_on_return_leg=False)
    values.update(overrides)
    return build_route_cost_config(**values)


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

    # ---- cost model -------------------------------------------------------------------------

    def test_fuel_is_km_per_liter_times_diesel_price_and_counted_once(self):
        # 14 km / 7 km/L * P95 = P190 fuel; distance cost is separate and does not include fuel.
        breakdown = compute_route_cost_breakdown(14.0, 60.0, config=make_config(distance_rate_per_km=5.0))
        self.assertEqual(breakdown["fuel"], 190.0)
        self.assertEqual(breakdown["distance"], 70.0)
        self.assertEqual(breakdown["time"], 120.0)
        self.assertEqual(breakdown["refrigeration"], 0.0)
        self.assertEqual(breakdown["total"], 190.0 + 70.0 + 120.0)

    def test_helper_cost_only_applies_when_a_helper_is_assigned(self):
        config = make_config(driver_cost_per_hour=120.0, helper_cost_per_hour=100.0)
        self.assertEqual(compute_route_cost_breakdown(0, 60, config=config)["time"], 120.0)
        self.assertEqual(compute_route_cost_breakdown(0, 60, config=config, has_helper=True)["time"], 220.0)

    def test_refrigeration_chilled_vs_frozen_and_non_reefer(self):
        config = make_config()  # 0.8 L/h chilled, 1.2 L/h frozen, P95/L
        chilled = compute_route_cost_breakdown(0, 0, config=config, refrigeration_hours=2.0)
        frozen = compute_route_cost_breakdown(0, 0, config=config, refrigeration_hours=2.0, frozen=True)
        none = compute_route_cost_breakdown(0, 0, config=config, refrigeration_hours=2.0, refrigerated=False)
        self.assertEqual(chilled["refrigeration"], round(2 * 0.8 * 95, 2))
        self.assertEqual(frozen["refrigeration"], round(2 * 1.2 * 95, 2))
        self.assertEqual(none["refrigeration"], 0.0)
        self.assertEqual(none["total"], 0.0)

    def test_rates_description_uses_live_config(self):
        rates = describe_rates(make_config())
        self.assertEqual(rates["fuelCostPerKm"], round(95 / 7, 2))
        self.assertEqual(rates["refrigerationCostPerHourChilled"], 76.0)
        self.assertEqual(rates["refrigerationCostPerHourFrozen"], 114.0)

    # ---- round trip split -------------------------------------------------------------------

    def test_round_trip_per_leg_totals_sum_to_total_and_return_has_no_refrigeration(self):
        config = make_config(distance_rate_per_km=5.0)
        legs = [{"distance_km": 12.3, "duration_min": 30}, {"distance_km": 8.9, "duration_min": 18}]
        split = _split_round_trip(legs, True, config, service_min=30)
        outbound, ret, total = split["outbound"], split["return"], split["total"]
        self.assertEqual(outbound["distanceKm"], 12.3)
        self.assertEqual(ret["distanceKm"], 8.9)
        self.assertEqual(total["distanceKm"], 21.2)
        self.assertEqual(total["durationMin"], 48.0)
        self.assertEqual(ret["costBreakdown"]["refrigeration"], 0.0)
        # outbound refrigeration = (30 driving + 30 service) min = 1h chilled
        self.assertEqual(outbound["costBreakdown"]["refrigeration"], 76.0)
        for key in ("distance", "time", "fuel", "refrigeration", "total"):
            self.assertAlmostEqual(total["costBreakdown"][key], outbound["costBreakdown"][key] + ret["costBreakdown"][key], places=2)
        b = outbound["costBreakdown"]
        self.assertAlmostEqual(b["total"], b["distance"] + b["time"] + b["fuel"] + b["refrigeration"], places=2)

    def test_return_leg_refrigeration_flag_on(self):
        config = make_config(refrigeration_on_return_leg=True)
        split = _split_round_trip([{"distance_km": 1, "duration_min": 60}, {"distance_km": 1, "duration_min": 30}], True, config)
        self.assertEqual(split["return"]["costBreakdown"]["refrigeration"], round(0.5 * 0.8 * 95, 2))

    def test_one_way_has_no_return_leg(self):
        split = _split_round_trip([{"distance_km": 10, "duration_min": 20}], False, make_config())
        self.assertIsNone(split["return"])
        self.assertEqual(split["total"], split["outbound"])

    # ---- warehouses -------------------------------------------------------------------------

    def test_no_default_return_warehouse(self):
        self.assertIsNone(resolve_return_warehouse(False, None))  # one-way: none needed
        self.assertIsNone(resolve_return_warehouse(False, "mets"))
        with self.assertRaises(ReturnWarehouseRequired):
            resolve_return_warehouse(True, None)
        with self.assertRaises(ReturnWarehouseRequired):
            resolve_return_warehouse(True, "  ")
        self.assertEqual(resolve_return_warehouse(True, "glacier")["id"], "glacier")
        with self.assertRaises(KeyError):
            resolve_return_warehouse(True, "nope")

    def test_warehouse_config_coordinates(self):
        mets, glacier = get_warehouse("mets"), get_warehouse("glacier")
        self.assertEqual((mets["lat"], mets["lng"]), (14.2907776, 121.0134132))
        self.assertEqual((glacier["lat"], glacier["lng"]), (14.4922771, 120.9929815))

    def test_plan_route_requires_a_chosen_warehouse_when_checked(self):
        body = RoutePlanBody(origin="A", destination="B", originLat=14.5, originLng=121.0, destinationLat=14.6, destinationLng=121.1, returnToWarehouse=True)
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(plan_route(body))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Choose the return warehouse", ctx.exception.detail)

    # ---- Routes API request + plan_route (mocked Google) -------------------------------------

    def _plan(self, **overrides):
        captured = {}

        async def fake_polyline(locations, *, optimize_waypoint_order=False):
            captured["locations"] = locations
            captured["optimize"] = optimize_waypoint_order
            n = len(locations) - 1
            legs = [{"distance_km": 10.0, "duration_min": 20.0} for _ in range(n)]
            return {"polyline": "{}", "skippedReason": None, "provider": "google", "profile": "DRIVE", "traffic_aware": True,
                    "distance_km": 10.0 * n, "duration_min": 20.0 * n, "legs": legs,
                    "optimized_waypoint_order": overrides.pop("_order", [])}

        async def fake_matrix(locations):
            n = len(locations)
            return {"distance_matrix_km": [[1.0] * n] * n, "duration_matrix_min": [[1.0] * n] * n, "provider": "google", "profile": "DRIVE", "traffic_aware": True}

        async def no_ferry(_locations):
            return None

        params = dict(origin="Start", destination="Last", originLat=14.5, originLng=121.0, destinationLat=14.6, destinationLng=121.1,
                      stops=[{"label": "S1", "lat": 14.55, "lng": 121.05}])
        params.update(overrides)
        with mock.patch("routers.routes.fetch_route_polyline", fake_polyline), mock.patch("routers.routes.fetch_route_matrix", fake_matrix), mock.patch("routers.routes.route_requires_ferry", no_ferry), mock.patch.dict(os.environ, ENV_RATES):
            result = asyncio.run(plan_route(RoutePlanBody(**params)))
        return result, captured

    def test_round_trip_request_destination_is_warehouse_and_top_card_equals_total(self):
        result, captured = self._plan(returnToWarehouse=True, returnWarehouseId="glacier")
        glacier = get_warehouse("glacier")
        self.assertEqual(captured["locations"][-1], [glacier["lng"], glacier["lat"]])
        self.assertEqual(len(captured["locations"]), 4)  # origin, S1, Last, warehouse
        self.assertFalse(captured["optimize"])
        trip = result["roundTrip"]
        self.assertEqual(trip["outbound"]["distanceKm"], 20.0)  # origin->S1->Last
        self.assertEqual(trip["return"]["distanceKm"], 10.0)    # Last->warehouse
        self.assertEqual(result["cost"], trip["total"]["costBreakdown"]["total"])
        self.assertEqual(result["costBreakdown"], trip["total"]["costBreakdown"])
        self.assertAlmostEqual(result["cost"], trip["outbound"]["costBreakdown"]["total"] + trip["return"]["costBreakdown"]["total"], places=2)
        self.assertEqual(result["returnWarehouse"]["id"], "glacier")

    def test_one_way_request_destination_is_last_stop_and_only_outbound(self):
        result, captured = self._plan(returnToWarehouse=False)
        self.assertEqual(captured["locations"][-1], [121.1, 14.6])
        self.assertEqual(len(captured["locations"]), 3)
        self.assertIsNone(result["roundTrip"]["return"])
        self.assertFalse(result["returnToWarehouse"])
        self.assertEqual(result["cost"], result["roundTrip"]["outbound"]["costBreakdown"]["total"])

    def test_optimize_stop_order_keeps_warehouse_as_fixed_destination(self):
        stops = [{"label": "S1", "lat": 14.55, "lng": 121.05}, {"label": "S2", "lat": 14.56, "lng": 121.06}]
        # Google returns the new order of the intermediates [S1, S2, Last] -> [Last, S1, S2]
        result, captured = self._plan(returnToWarehouse=True, returnWarehouseId="mets", optimizeStopOrder=True, stops=stops, _order=[2, 0, 1])
        mets = get_warehouse("mets")
        self.assertTrue(captured["optimize"])
        self.assertEqual(captured["locations"][-1], [mets["lng"], mets["lat"]])
        self.assertEqual([s["label"] for s in result["stops"]] + [result["destination"]["label"]], ["Last", "S1", "S2"])
        self.assertEqual(result["destination"]["label"], "S2")

    def test_refrigeration_assumptions_flow_into_plan(self):
        chilled, _ = self._plan(returnToWarehouse=True, returnWarehouseId="mets")
        frozen, _ = self._plan(returnToWarehouse=True, returnWarehouseId="mets", coldChainCategory="frozen")
        dry, _ = self._plan(returnToWarehouse=True, returnWarehouseId="mets", refrigerated=False)
        self.assertTrue(chilled["costAssumptions"]["coldChainAssumed"])
        self.assertGreater(frozen["costBreakdown"]["refrigeration"], chilled["costBreakdown"]["refrigeration"])
        self.assertEqual(dry["costBreakdown"]["refrigeration"], 0.0)
        self.assertEqual(chilled["roundTrip"]["return"]["costBreakdown"]["refrigeration"], 0.0)

    def test_routes_api_request_body(self):
        captured = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"routes": [{"distanceMeters": 1000, "duration": "60s", "polyline": {"encodedPolyline": "_p~iF~ps|U"}, "legs": [], "optimizedIntermediateWaypointIndex": [1, 0]}]}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None):
                captured["body"] = json
                response = FakeResponse()
                response.request = SimpleNamespace(url=SimpleNamespace(path="/x"))
                return response

        with mock.patch("services.google_maps.httpx.AsyncClient", FakeClient), mock.patch.dict(os.environ, {"GOOGLE_MAPS_API_KEY": "k"}):
            result = asyncio.run(fetch_route_polyline([[121.0, 14.5], [121.1, 14.6], [121.2, 14.7], [121.3, 14.8]], optimize_waypoint_order=True))
        body = captured["body"]
        self.assertTrue(body["optimizeWaypointOrder"])
        self.assertEqual(body["destination"]["location"]["latLng"]["longitude"], 121.3)
        self.assertEqual(len(body["intermediates"]), 2)
        self.assertEqual(result["optimized_waypoint_order"], [1, 0])

    # ---- Route Optimization API + OR-Tools ---------------------------------------------------

    def test_google_optimization_end_location_only_when_return_checked(self):
        vehicle = SimpleNamespace(id=1, start_lat=14.0, start_lng=121.0, end_lat=15.0, end_lng=122.0, has_end_location=True, capacity_kg=1000, cost_per_km=10, cost_per_hour=100, shift_start=0, shift_end=1440, temperature_capabilities=["ambient"])
        shipment = SimpleNamespace(id=10, lat=14.5, lng=121.5, service_time_min=15, time_window_start=0, time_window_end=1440, demand_kg=1)
        request = build_request(SimpleNamespace(vehicles=[vehicle], shipments=[shipment]))
        self.assertEqual(request["model"]["vehicles"][0]["endLocation"], {"latitude": 15.0, "longitude": 122.0})
        vehicle.has_end_location = False
        request = build_request(SimpleNamespace(vehicles=[vehicle], shipments=[shipment]))
        self.assertNotIn("endLocation", request["model"]["vehicles"][0])

    def test_google_optimization_costs_include_fuel_per_km(self):
        vehicle = SimpleNamespace(id=1, start_lat=14.0, start_lng=121.0, end_lat=15.0, end_lng=122.0, has_end_location=False, capacity_kg=1000, cost_per_km=None, cost_per_hour=None, shift_start=0, shift_end=1440, temperature_capabilities=["ambient"])
        with mock.patch.dict(os.environ, ENV_RATES):
            request = build_request(SimpleNamespace(vehicles=[vehicle], shipments=[]))
        v = request["model"]["vehicles"][0]
        self.assertAlmostEqual(v["costPerKilometer"], 5.0 + 95 / 7, places=6)
        self.assertEqual(v["costPerHour"], 120.0)

    def test_ortools_arc_cost_matches_single_route_leg_cost(self):
        config = make_config(distance_rate_per_km=5.0)
        km, minutes = 12.3, 30.0
        for refrigerated in (False, True):
            with self.subTest(refrigerated=refrigerated):
                matrix = build_objective_cost_matrix([[0, km], [km, 0]], [[0, minutes], [minutes, 0]], objective="cheapest", config=config, refrigerated=refrigerated)
                leg = compute_route_cost_breakdown(km, minutes, config=config, refrigeration_hours=minutes / 60.0, refrigerated=refrigerated)
                self.assertAlmostEqual(matrix[0][1], leg["total"], delta=0.03)

    def test_ortools_vehicle_matrix_uses_vehicle_rate_overrides_plus_fuel(self):
        config = make_config(distance_rate_per_km=5.0)
        vehicle = optimizer.Vehicle(id="V", capacity_kg=1000, start_node=0, end_node=0, shift_start=0, shift_end=1440, temperature_capabilities=["ambient"], cost_per_km=8.0, cost_per_hour=60.0)
        matrix = optimizer.compute_cost_matrix(objective="cheapest", distance_km=[[0, 7], [7, 0]], duration_min=[[0, 60], [60, 0]], vehicle=vehicle,
                                               configured_distance_rate=5.0, configured_time_rate=120.0, config=config,
                                               balanced_time_weight=0.5, balanced_distance_weight=0.2, balanced_cost_weight=0.3)
        self.assertAlmostEqual(matrix[0][1], 7 * (8.0 + 95 / 7) + 60.0, places=6)

    def test_refrigeration_cost_names_are_gone_from_config(self):
        config = load_route_cost_config()
        self.assertNotIn("refrigeration_cost_per_hour", config)
        self.assertNotIn("fuel_surcharge_per_km", config)
        self.assertNotIn("fixed_cost_per_route", config)


if __name__ == "__main__":
    unittest.main()
