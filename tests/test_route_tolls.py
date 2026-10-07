import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("JWT_SECRET", "test")

from routers.routes import RoutePlanBody, _split_round_trip, plan_route
from services.google_maps import fetch_route_polyline, parse_toll_info
from services.google_route_optimization import build_request
from services.route_costs import build_route_cost_config, compute_route_cost_breakdown, describe_rates, leg_toll, load_route_cost_config

ENV = {
    "ROUTE_DIESEL_PRICE_PER_LITER": "95", "ROUTE_FUEL_KM_PER_LITER": "7", "ROUTE_DRIVER_COST_PER_HOUR": "120",
    "ROUTE_HELPER_COST_PER_HOUR": "120", "ROUTE_DISTANCE_COST_PER_KM": "5",
    "ROUTE_REFRIGERATION_LITERS_PER_HOUR_CHILLED": "0.8", "ROUTE_REFRIGERATION_LITERS_PER_HOUR_FROZEN": "1.2",
    "ROUTE_REFRIGERATION_ON_RETURN_LEG": "false",
    "ROUTE_TOLLS_ENABLED": "true", "ROUTE_TOLL_VEHICLE_CLASS": "2",
    "ROUTE_TOLL_CLASS2_MULTIPLIER": "2.0", "ROUTE_TOLL_CLASS3_MULTIPLIER": "3.0",
}


def config(**overrides):
    values = dict(distance_rate_per_km=5.0, diesel_price_per_liter=95.0, fuel_km_per_liter=7.0, driver_cost_per_hour=120.0,
                  helper_cost_per_hour=120.0, refrigeration_liters_per_hour_chilled=0.8, refrigeration_liters_per_hour_frozen=1.2)
    values.update(overrides)
    return build_route_cost_config(**values)


def money(units, nanos=0, currency="PHP"):
    return {"currencyCode": currency, "units": str(units), "nanos": nanos}


class TollParsingAndCostTests(unittest.TestCase):
    def test_google_price_is_read_as_php_units_plus_nanos(self):
        info = parse_toll_info({"tollInfo": {"estimatedPrice": [money(39, 500_000_000)]}})
        self.assertEqual(info, {"present": True, "price": 39.5})

    def test_toll_without_a_price_is_present_but_unknown(self):
        self.assertEqual(parse_toll_info({"tollInfo": {}}), {"present": True, "price": None})
        self.assertEqual(parse_toll_info({"tollInfo": {"estimatedPrice": [money(5, 0, "USD")]}}), {"present": True, "price": None})

    def test_no_toll_info_means_no_toll(self):
        self.assertEqual(parse_toll_info({}), {"present": False, "price": None})
        self.assertEqual(parse_toll_info(None), {"present": False, "price": None})

    def test_price_is_multiplied_by_the_vehicle_class(self):
        legs = {1: 39.0, 2: 78.0, 3: 117.0}  # CAVITEX P39 / P78 / P117
        for vehicle_class, expected in legs.items():
            cfg = config(toll_vehicle_class=vehicle_class)
            self.assertEqual(leg_toll({"present": True, "price": 39.0}, cfg)["amount"], expected, vehicle_class)
        self.assertEqual(leg_toll({"present": True, "price": 105.0}, config(toll_vehicle_class=2))["amount"], 210.0)  # Skyway Buendia-Quirino
        self.assertEqual(leg_toll({"present": True, "price": 23.0}, config(toll_vehicle_class=3))["amount"], 69.0)  # MCX 23 x 3 (TRB lists 70 after rounding)

    def test_env_config_defaults_to_class_2_times_two(self):
        with mock.patch.dict(os.environ, ENV):
            cfg = load_route_cost_config()
        self.assertEqual((cfg["tolls_enabled"], cfg["toll_vehicle_class"], cfg["toll_multiplier"]), (True, 2, 2.0))
        rates = describe_rates(cfg)
        self.assertEqual((rates["tollsEnabled"], rates["tollVehicleClass"], rates["tollMultiplier"]), (True, 2, 2.0))

    def test_env_overrides_apply(self):
        with mock.patch.dict(os.environ, {**ENV, "ROUTE_TOLL_VEHICLE_CLASS": "3", "ROUTE_TOLL_CLASS3_MULTIPLIER": "3.5"}):
            self.assertEqual(load_route_cost_config()["toll_multiplier"], 3.5)
        with mock.patch.dict(os.environ, {**ENV, "ROUTE_TOLL_VEHICLE_CLASS": "1"}):
            self.assertEqual(load_route_cost_config()["toll_multiplier"], 1.0)

    def test_unknown_fee_is_flagged_and_never_costed_as_zero_peso_toll(self):
        toll = leg_toll({"present": True, "price": None}, config())
        self.assertEqual(toll, {"present": True, "amount": 0.0, "unknown": True})
        self.assertEqual(leg_toll({"present": False, "price": None}, config()), {"present": False, "amount": 0.0, "unknown": False})
        self.assertEqual(leg_toll(None, config())["amount"], 0.0)

    def test_disabled_tolls_cost_nothing(self):
        self.assertFalse(leg_toll({"present": True, "price": 50.0}, config(tolls_enabled=False))["present"])

    def test_tolls_are_part_of_the_leg_total(self):
        base = compute_route_cost_breakdown(14.0, 60.0, config=config())
        with_toll = compute_route_cost_breakdown(14.0, 60.0, config=config(), tolls=78.0)
        self.assertEqual(with_toll["tolls"], 78.0)
        self.assertEqual(with_toll["total"], round(base["total"] + 78.0, 2))
        self.assertEqual(base["tolls"], 0.0)

    def test_per_leg_tolls_roll_into_outbound_return_and_total(self):
        legs = [
            {"distance_km": 10, "duration_min": 20, "toll": {"present": True, "price": 39.0}},
            {"distance_km": 10, "duration_min": 20, "toll": {"present": True, "price": 20.0}},
            {"distance_km": 10, "duration_min": 20, "toll": {"present": False, "price": None}},
        ]
        split = _split_round_trip(legs, True, config())
        self.assertEqual(split["outbound"]["costBreakdown"]["tolls"], 78.0 + 40.0)
        self.assertEqual(split["return"]["costBreakdown"]["tolls"], 0.0)  # no toll on the return leg
        self.assertFalse(split["return"]["toll"]["present"])
        self.assertEqual(split["total"]["costBreakdown"]["tolls"], 118.0)
        for part in (split["outbound"], split["return"]):
            b = part["costBreakdown"]
            self.assertAlmostEqual(b["total"], b["distance"] + b["time"] + b["fuel"] + b["refrigeration"] + b["tolls"], places=2)
        self.assertAlmostEqual(split["total"]["costBreakdown"]["total"], split["outbound"]["costBreakdown"]["total"] + split["return"]["costBreakdown"]["total"], places=2)

    def test_unknown_leg_fee_is_excluded_from_the_total_and_flagged(self):
        legs = [
            {"distance_km": 10, "duration_min": 20, "toll": {"present": True, "price": None}},
            {"distance_km": 10, "duration_min": 20, "toll": {"present": True, "price": 39.0}},
        ]
        split = _split_round_trip(legs, True, config())
        self.assertTrue(split["outbound"]["toll"]["unknown"])
        self.assertEqual(split["outbound"]["costBreakdown"]["tolls"], 0.0)
        self.assertFalse(split["return"]["toll"]["unknown"])
        self.assertEqual(split["return"]["costBreakdown"]["tolls"], 78.0)
        self.assertTrue(split["total"]["toll"]["unknown"])  # TOTAL gets the "+ tolls (unknown)" note
        self.assertEqual(split["total"]["costBreakdown"]["tolls"], 78.0)

    def test_legs_without_toll_data_have_zero_tolls(self):
        split = _split_round_trip([{"distance_km": 10, "duration_min": 20}], False, config())
        self.assertEqual(split["outbound"]["costBreakdown"]["tolls"], 0.0)
        self.assertFalse(split["outbound"]["toll"]["present"])


class RoutesApiRequestTests(unittest.TestCase):
    def _call(self, **kwargs):
        captured = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"routes": [{
                    "distanceMeters": 20000, "duration": "1200s", "polyline": {"encodedPolyline": "_p~iF~ps|U"},
                    "travelAdvisory": {"tollInfo": {"estimatedPrice": [money(39)]}},
                    "legs": [
                        {"distanceMeters": 12000, "duration": "700s", "travelAdvisory": {"tollInfo": {"estimatedPrice": [money(39)]}}},
                        {"distanceMeters": 8000, "duration": "500s", "travelAdvisory": {"tollInfo": {}}},
                        {"distanceMeters": 1000, "duration": "60s"},
                    ],
                }]}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

            async def post(self, url, json=None, headers=None):
                captured["body"], captured["headers"] = json, headers
                response = FakeResponse()
                response.request = SimpleNamespace(url=SimpleNamespace(path="/x"))
                return response

        with mock.patch("services.google_maps.httpx.AsyncClient", FakeClient), mock.patch.dict(os.environ, {"GOOGLE_MAPS_API_KEY": "k"}):
            result = asyncio.run(fetch_route_polyline([[121.0, 14.5], [121.1, 14.6], [121.2, 14.7]], **kwargs))
        return result, captured

    def test_tolls_are_requested_with_extra_computation_and_field_mask(self):
        result, captured = self._call(include_tolls=True)
        self.assertEqual(captured["body"]["extraComputations"], ["TOLLS"])
        mask = captured["headers"]["X-Goog-FieldMask"]
        self.assertIn("routes.travelAdvisory.tollInfo", mask)
        self.assertIn("routes.legs.travelAdvisory.tollInfo", mask)
        self.assertNotIn("routeModifiers", captured["body"])
        self.assertTrue(result["tolls_requested"])

    def test_tolls_are_not_requested_by_default(self):
        result, captured = self._call()
        self.assertNotIn("extraComputations", captured["body"])
        self.assertNotIn("tollInfo", captured["headers"]["X-Goog-FieldMask"])
        self.assertNotIn("toll", result["legs"][0])
        self.assertFalse(result["tolls_requested"])

    def test_avoid_tolls_sets_the_route_modifier(self):
        _, captured = self._call(avoid_tolls=True)
        self.assertEqual(captured["body"]["routeModifiers"], {"avoidTolls": True})
        self.assertNotIn("extraComputations", captured["body"])

    def test_leg_toll_info_is_parsed_per_leg(self):
        result, _ = self._call(include_tolls=True, avoid_tolls=True)
        tolls = [leg["toll"] for leg in result["legs"]]
        self.assertEqual(tolls, [{"present": True, "price": 39.0}, {"present": True, "price": None}, {"present": False, "price": None}])


class PlanRouteTollTests(unittest.TestCase):
    def _plan(self, env=None, **overrides):
        calls = []

        async def fake_polyline(locations, *, optimize_waypoint_order=False, include_tolls=False, avoid_tolls=False):
            calls.append({"locations": locations, "optimize": optimize_waypoint_order, "tolls": include_tolls, "avoid": avoid_tolls})
            n = len(locations) - 1
            if avoid_tolls:  # longer, slower, no toll
                legs = [{"distance_km": 14.0, "duration_min": 30.0, "toll": {"present": False, "price": None}} for _ in range(n)]
            else:  # expressway: first leg is tolled at P39 (Class 1)
                legs = [{"distance_km": 10.0, "duration_min": 20.0, "toll": {"present": i == 0, "price": 39.0 if i == 0 else None}} for i in range(n)]
            if not include_tolls:
                legs = [{k: v for k, v in leg.items() if k != "toll"} for leg in legs]
            return {"polyline": "AVOID" if avoid_tolls else "EXPRESSWAY", "skippedReason": None, "provider": "google", "profile": "DRIVE", "traffic_aware": True,
                    "distance_km": sum(l["distance_km"] for l in legs), "duration_min": sum(l["duration_min"] for l in legs), "legs": legs,
                    "optimized_waypoint_order": []}

        async def fake_matrix(locations):
            n = len(locations)
            return {"distance_matrix_km": [[1.0] * n] * n, "duration_matrix_min": [[1.0] * n] * n, "provider": "google", "profile": "DRIVE", "traffic_aware": True}

        async def no_ferry(_locations):
            return None

        params = dict(origin="Start", destination="Last", originLat=14.5, originLng=121.0, destinationLat=14.6, destinationLng=121.1, returnToWarehouse=False)
        params.update(overrides)
        with mock.patch("routers.routes.fetch_route_polyline", fake_polyline), mock.patch("routers.routes.fetch_route_matrix", fake_matrix), \
                mock.patch("routers.routes.route_requires_ferry", no_ferry), mock.patch.dict(os.environ, {**ENV, **(env or {})}):
            result = asyncio.run(plan_route(RoutePlanBody(**params)))
        return result, calls

    def test_class_2_toll_is_google_price_times_two_in_the_row_and_total(self):
        result, calls = self._plan(expressways="expressway")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["roundTrip"]["outbound"]["costBreakdown"]["tolls"], 78.0)
        self.assertEqual(result["costBreakdown"]["tolls"], 78.0)
        b = result["costBreakdown"]
        self.assertAlmostEqual(result["cost"], b["distance"] + b["time"] + b["fuel"] + b["refrigeration"] + b["tolls"], places=2)
        self.assertEqual(result["rates"]["tollMultiplier"], 2.0)

    def test_class_1_vehicle_uses_the_google_price_as_is(self):
        result, _ = self._plan(env={"ROUTE_TOLL_VEHICLE_CLASS": "1"}, expressways="expressway")
        self.assertEqual(result["costBreakdown"]["tolls"], 39.0)

    def test_compare_both_makes_two_calls_and_returns_two_options(self):
        result, calls = self._plan(expressways="compare")
        self.assertEqual(len(calls), 2)
        self.assertEqual([c["avoid"] for c in calls], [False, True])
        self.assertTrue(all(c["tolls"] for c in calls))
        keys = [o["key"] for o in result["tollOptions"]]
        self.assertEqual(keys, ["expressway", "avoid"])
        expressway, avoid = result["tollOptions"]
        self.assertEqual(expressway["costBreakdown"]["tolls"], 78.0)
        self.assertEqual(avoid["costBreakdown"]["tolls"], 0.0)
        self.assertLess(expressway["durationMin"], avoid["durationMin"])
        self.assertEqual(result["fastestOption"], "expressway")
        self.assertEqual(result["cheapestOption"], min(result["tollOptions"], key=lambda o: o["cost"])["key"])
        self.assertEqual(expressway["geometry"], "EXPRESSWAY")
        self.assertEqual(avoid["geometry"], "AVOID")

    def test_the_default_active_option_follows_the_objective_and_drives_the_top_level_plan(self):
        fastest, _ = self._plan(expressways="compare", mode="fastest")
        shortest, _ = self._plan(expressways="compare", mode="shortest")
        by_key = {o["key"]: o for o in fastest["tollOptions"]}
        self.assertEqual(fastest["activeOption"], "expressway")
        self.assertEqual(fastest["cost"], by_key["expressway"]["cost"])
        self.assertEqual(fastest["roundTrip"], by_key["expressway"]["roundTrip"])
        self.assertEqual(fastest["geometry"], "EXPRESSWAY")
        self.assertEqual(shortest["activeOption"], "expressway")  # 20 km vs 28 km one-way pair
        cheapest, _ = self._plan(expressways="compare", mode="cheapest")
        totals = {o["key"]: o["cost"] for o in cheapest["tollOptions"]}
        self.assertEqual(cheapest["activeOption"], min(totals, key=totals.get))
        self.assertEqual(cheapest["cost"], totals[cheapest["activeOption"]])

    def test_cheapest_and_balanced_compare_totals_that_include_tolls(self):
        # Big toll: the expressway is faster but its total (with the toll) is higher than the detour.
        pricey = {"ROUTE_TOLL_CLASS2_MULTIPLIER": "50"}
        cheapest, _ = self._plan(env=pricey, expressways="compare", mode="cheapest")
        self.assertEqual(cheapest["activeOption"], "avoid")
        self.assertEqual(cheapest["cheapestOption"], "avoid")
        self.assertEqual(cheapest["fastestOption"], "expressway")
        balanced, _ = self._plan(env=pricey, expressways="compare", mode="balanced")
        self.assertEqual(balanced["activeOption"], "avoid")

    def test_avoid_tolls_only_sets_the_modifier_on_a_single_call(self):
        result, calls = self._plan(expressways="avoid")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["avoid"])
        self.assertEqual(result["tollOptions"], [])
        self.assertEqual(result["activeOption"], "avoid")
        self.assertEqual(result["geometry"], "AVOID")

    def test_use_expressways_makes_one_normal_call(self):
        result, calls = self._plan(expressways="expressway")
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["avoid"])
        self.assertEqual(result["tollOptions"], [])

    def test_tolls_are_requested_on_calculate(self):
        _, calls = self._plan(expressways="expressway")
        self.assertTrue(calls[0]["tolls"])

    def test_disabled_tolls_request_nothing_and_compare_falls_back_to_one_call(self):
        result, calls = self._plan(env={"ROUTE_TOLLS_ENABLED": "false"}, expressways="compare")
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["tolls"])
        self.assertFalse(calls[0]["avoid"])
        self.assertFalse(result["tollsEnabled"])
        self.assertEqual(result["tollOptions"], [])
        self.assertEqual(result["costBreakdown"]["tolls"], 0.0)

    def test_compare_keeps_the_same_stop_order_when_google_reorders_the_stops(self):
        calls = []

        async def fake_polyline(locations, *, optimize_waypoint_order=False, include_tolls=False, avoid_tolls=False):
            calls.append((locations, optimize_waypoint_order, avoid_tolls))
            return {"polyline": "{}", "skippedReason": None, "provider": "google", "profile": "DRIVE", "traffic_aware": True, "legs": [{"distance_km": 1, "duration_min": 1}] * (len(locations) - 1),
                    "optimized_waypoint_order": [1, 0] if optimize_waypoint_order else []}

        async def fake_matrix(locations):
            n = len(locations)
            return {"distance_matrix_km": [[1.0] * n] * n, "duration_matrix_min": [[1.0] * n] * n, "provider": "google"}

        async def no_ferry(_locations):
            return None

        body = RoutePlanBody(origin="S", destination="D", originLat=14.5, originLng=121.0, destinationLat=14.6, destinationLng=121.1, returnToWarehouse=False,
                             stops=[{"label": "A", "lat": 14.51, "lng": 121.01}, {"label": "B", "lat": 14.52, "lng": 121.02}], optimizeStopOrder=True, expressways="compare")
        with mock.patch("routers.routes.fetch_route_polyline", fake_polyline), mock.patch("routers.routes.fetch_route_matrix", fake_matrix), \
                mock.patch("routers.routes.route_requires_ferry", no_ferry), mock.patch.dict(os.environ, ENV):
            result = asyncio.run(plan_route(body))
        (first_locations, first_optimize, _), (second_locations, second_optimize, second_avoid) = calls
        self.assertTrue(first_optimize)
        self.assertFalse(second_optimize)  # the second call reuses the order the first one found
        self.assertTrue(second_avoid)
        self.assertEqual(second_locations[1], [121.02, 14.52])  # B first, as reordered by Google
        self.assertEqual([s["label"] for s in result["stops"]], ["B", "A"])


class FleetOptimizationTollTests(unittest.TestCase):
    def _request(self, **kwargs):
        vehicle = SimpleNamespace(id=1, start_lat=14.0, start_lng=121.0, end_lat=15.0, end_lng=122.0, has_end_location=True, capacity_kg=1000,
                                  cost_per_km=10, cost_per_hour=100, shift_start=0, shift_end=1440, temperature_capabilities=["ambient"])
        return build_request(SimpleNamespace(vehicles=[vehicle], shipments=[]), **kwargs)["model"]["vehicles"][0]

    def test_avoid_tolls_sets_the_vehicle_route_modifier(self):
        self.assertEqual(self._request(avoid_tolls=True)["routeModifiers"], {"avoidTolls": True})

    def test_tolls_are_allowed_by_default(self):
        self.assertNotIn("routeModifiers", self._request())


if __name__ == "__main__":
    unittest.main()
