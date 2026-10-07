"""Inventory On Hold filter, acknowledge-state consistency, and over-capacity warnings.

No live Zoho call anywhere: a small in-memory fake stands in for the Zoho client functions, and
the real cache code (live_sales_order_cache / load_planning ack cache) runs against it.
"""
import asyncio
import os
import unittest
from contextlib import ExitStack
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET", "test")

from auth.dependencies import CurrentUser
from routers import assignment, load_planning, pipeline
from services import live_sales_order_cache as lc
from services.zoho_client import ZohoError

SHIP = "2026-10-06"


class FakeZoho:
    """Zoho's side: the real order state, plus an Acknowledged custom view that can lag it."""

    def __init__(self, count=10, on_hold=()):
        self.orders = {}
        for n in range(1, count + 1):
            sub = ("cs_onholds" if n == 7 else "cs_onhold") if n in on_hold else "confirmed"
            self.orders[f"id{n}"] = {"salesorder_id": f"id{n}", "salesorder_number": f"SO-{n:02d}", "customer_name": f"Cust {n}", "status": "confirmed", "current_sub_status": sub, "order_sub_status": sub, "shipment_date": SHIP, "last_modified_time": "t0", "line_items": []}
        self.view_lags = False
        self.view_snapshot = set()
        self.fail_ack = set()
        self.calls = {"window_list": 0, "detail": 0, "view": 0, "ack_post": 0, "remove_post": 0}

    def acked(self):
        return {i for i, o in self.orders.items() if o["current_sub_status"] == "cs_acknowl"}

    # --- patched Zoho client functions ---
    def window_list(self, start, end, page=1, per_page=200):
        self.calls["window_list"] += 1
        return {"salesorders": [{k: v for k, v in o.items() if k != "line_items"} for o in self.orders.values()], "page_context": {"has_more_page": False, "search_criteria": [{"column_name": "shipment_date"}]}}

    def detail(self, order_id):
        self.calls["detail"] += 1
        return {"salesorder": dict(self.orders[order_id])}

    def view(self, customview_id, page=1, per_page=200):
        self.calls["view"] += 1
        ids = self.view_snapshot if self.view_lags else self.acked()
        return {"salesorders": [{"salesorder_id": i} for i in sorted(ids)], "page_context": {"has_more_page": False}}

    def ack(self, order_id, status_code="cs_acknowl"):
        self.calls["ack_post"] += 1
        if order_id in self.fail_ack:
            raise ZohoError("Zoho refused")
        self.orders[order_id]["current_sub_status"] = self.orders[order_id]["order_sub_status"] = "cs_acknowl"
        self.orders[order_id]["last_modified_time"] = f"ack-{self.calls['ack_post']}"
        return {"code": 0}

    def remove_ack(self, order_id):
        self.calls["remove_post"] += 1
        self.orders[order_id]["current_sub_status"] = self.orders[order_id]["order_sub_status"] = "confirmed"
        self.orders[order_id]["last_modified_time"] = f"remove-{self.calls['remove_post']}"
        return {"code": 0}


class ZohoBackedTestCase(unittest.TestCase):
    ON_HOLD = ()
    COUNT = 10

    def setUp(self):
        self.zoho = FakeZoho(self.COUNT, self.ON_HOLD)
        self.user = CurrentUser(id=1, email="pau@example.com", role="dispatcher", full_name="Pau", status="active")
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target, attr, fake in (
            (lc, "fetch_sales_orders_by_shipment_date", self.zoho.window_list),
            (lc, "fetch_sales_order_detail", self.zoho.detail),
            (load_planning, "fetch_sales_order_detail", self.zoho.detail),
            (load_planning, "fetch_sales_orders_by_customview", self.zoho.view),
            (load_planning, "acknowledge_sales_order", self.zoho.ack),
            (load_planning, "remove_acknowledge_sales_order", self.zoho.remove_ack),
            (load_planning, "lock_salesorder", lambda *a, **k: {"locked": True, "already_locked": False, "lock_status": {}, "lock_error": None}),
            (load_planning, "stock_for_orders_cached", lambda rows: ({}, 0)),
        ):
            stack.enter_context(patch.object(target, attr, fake))
        self.reset_caches()
        self.addCleanup(self.reset_caches)

    def reset_caches(self):
        lc.invalidate_windows()
        lc.invalidate_assigned_zoho()
        with lc._detail_lock:
            lc._details.clear()
        load_planning._invalidate_ack_cache()
        load_planning._ack_overrides.clear()

    def restart_backend(self):
        """Everything in memory is gone; only Zoho remains."""
        self.reset_caches()

    def inventory(self, status="All except acknowledged", assignment=None, search=None):
        return load_planning.list_sales_orders(date_from=SHIP, date_to=SHIP, status=status, search=search, assignment=assignment, cities=None, vehicle=None, customer=None, delivery_status=None, page=1, per_page=100, db=SimpleNamespace())

    def numbers(self, page):
        return sorted(item["salesorder_number"] for item in page["items"])

    def acknowledge(self, *ids):
        """The frontend's bulk acknowledge: one single-acknowledge call per selected order."""
        out = {}
        for order_id in ids:
            try:
                out[order_id] = load_planning.acknowledge_sales_order_route(order_id, self.user)
            except Exception as exc:  # HTTPException from a Zoho failure
                out[order_id] = exc
        return out


class OnHoldFilterTests(ZohoBackedTestCase):
    ON_HOLD = (3, 7)

    def test_on_hold_excluded_from_list_and_counts(self):
        page = self.inventory()
        self.assertEqual(page["total"], 8)
        self.assertEqual(len(page["items"]), 8)
        self.assertFalse({"SO-03", "SO-07"} & set(self.numbers(page)))

    def test_non_on_hold_unaffected(self):
        page = self.inventory()
        self.assertEqual(self.numbers(page), [f"SO-{n:02d}" for n in (1, 2, 4, 5, 6, 8, 9, 10)])

    def test_every_inventory_status_filter_hides_on_hold(self):
        for status in ("All", "Confirmed", "All except acknowledged"):
            with self.subTest(status=status):
                self.reset_caches()
                self.assertFalse({"SO-03", "SO-07"} & set(self.numbers(self.inventory(status=status))))

    def test_search_cannot_surface_an_on_hold_order(self):
        self.assertEqual(self.inventory(search="SO-03")["total"], 0)

    def test_load_planning_excludes_on_hold(self):
        rows = load_planning._filtered_rows(SimpleNamespace(), SHIP, SHIP, None, None, "unassigned")
        self.assertEqual(len(rows), 8)
        self.assertFalse({"SO-03", "SO-07"} & {row.salesorder_number for row in rows})

    def test_on_hold_sales_substatus_is_on_hold(self):
        """Production: Zoho's "ON HOLD(SALES)" sub-status of Confirmed has code cs_onholds (not cs_onhold)."""
        sales = {"status": "confirmed", "current_sub_status": "cs_onholds", "order_sub_status": "cs_onholds", "current_sub_status_id": "4489499000021973444"}
        self.assertTrue(lc.is_on_hold(sales))
        self.assertTrue(lc.is_on_hold({"status": "confirmed", "current_sub_status": "cs_zzz", "current_sub_status_id": "9", "sub_statuses": [{"status_id": "9", "status_code": "cs_zzz", "display_name": "ON HOLD(SALES)"}]}))
        for ack in ({"status": "confirmed", "current_sub_status": "cs_acknowl", "current_sub_status_id": "1", "sub_statuses": [{"status_id": "1", "status_code": "cs_acknowl", "display_name": "ACKNOWLEDGED"}, {"status_id": "2", "status_code": "cs_onholds", "display_name": "ON HOLD(SALES)"}]},
                    {"status": "confirmed", "current_sub_status": "cs_dh1gl2e"}):
            self.assertFalse(lc.is_on_hold(ack))

    def test_is_on_hold_is_case_and_spacing_insensitive(self):
        for value in ("cs_onhold", "CS_ONHOLD", "On Hold", "on_hold", "ONHOLD"):
            self.assertTrue(lc.is_on_hold({"current_sub_status": value}), value)
        self.assertTrue(lc.is_on_hold({"order_sub_status": "CS_OnHold"}))
        for record in ({}, {"current_sub_status": "cs_acknowl"}, {"current_sub_status": "confirmed", "cf_sub_status_1": "ONHOLD"}):
            self.assertFalse(lc.is_on_hold(record), record)

    def test_row_objects_and_missing_raw_json(self):
        self.assertTrue(lc.is_on_hold(SimpleNamespace(raw_json={"current_sub_status": "cs_onhold"}, order_status="confirmed")))
        self.assertFalse(lc.is_on_hold(SimpleNamespace(raw_json=None, order_status="confirmed")))

    def test_weight_total_ignores_on_hold(self):
        with patch.object(load_planning, "_total_weight_kg", wraps=load_planning._total_weight_kg) as total:
            self.inventory()
        self.assertEqual({row.salesorder_number for row in total.call_args.args[0]} & {"SO-03", "SO-07"}, set())


class AcknowledgeLeavesInventoryTests(ZohoBackedTestCase):
    def test_inventory_only_lists_confirmed_unacknowledged_orders(self):
        self.zoho.orders["id3"]["status"] = "draft"
        self.zoho.orders["id4"]["status"] = "partially_shipped"
        self.zoho.orders["id5"]["status"] = "closed"
        self.zoho.orders["id6"]["current_sub_status"] = self.zoho.orders["id6"]["order_sub_status"] = "cs_onhold"
        self.zoho.orders["id7"]["current_sub_status"] = self.zoho.orders["id7"]["order_sub_status"] = "cs_acknowl"
        self.restart_backend()
        page = self.inventory(status="All")
        self.assertEqual(self.numbers(page), ["SO-01", "SO-02", "SO-08", "SO-09", "SO-10"])
        self.assertEqual(page["total"], 5)
        self.assertEqual(self.numbers(self.inventory(status="Confirmed")), ["SO-01", "SO-02", "SO-08", "SO-09", "SO-10"])
        self.assertEqual(self.numbers(self.inventory(status="Acknowledged")), [])

    def test_load_planning_still_lists_acknowledged_orders(self):
        self.zoho.orders["id2"]["current_sub_status"] = self.zoho.orders["id2"]["order_sub_status"] = "cs_acknowl"
        self.restart_backend()
        self.assertEqual(self.numbers(self.inventory(status="Acknowledged", assignment="unassigned")), ["SO-02"])

    def test_bulk_ack_two_of_ten_list_and_count_show_eight(self):
        before = self.inventory()
        self.assertEqual((len(before["items"]), before["total"]), (10, 10))
        results = self.acknowledge("id2", "id5")
        self.assertTrue(all(r["acknowledged"] for r in results.values()))
        after = self.inventory()
        self.assertEqual(after["total"], 8)
        self.assertEqual(len(after["items"]), 8)
        self.assertFalse({"SO-02", "SO-05"} & set(self.numbers(after)))
        self.assertEqual(self.numbers(self.inventory(status="Acknowledged")), [])
        self.assertEqual(self.numbers(self.inventory(status="Acknowledged", assignment="unassigned")), ["SO-02", "SO-05"])

    def test_acknowledged_orders_appear_in_load_planning(self):
        self.acknowledge("id2", "id5")
        page = self.inventory(status="Acknowledged", assignment="unassigned")
        self.assertEqual(self.numbers(page), ["SO-02", "SO-05"])

    def test_exclusion_survives_ack_cache_expiry_while_zoho_view_lags(self):
        self.inventory()  # warm the ack cache like a real session
        self.zoho.view_snapshot = set()
        self.zoho.view_lags = True  # Zoho's custom view has not indexed the change yet
        self.acknowledge("id2", "id5")
        load_planning._invalidate_ack_cache()  # TTL lapse / Refresh button
        self.assertEqual(self.inventory()["total"], 8)
        self.assertEqual(self.numbers(self.inventory(status="Acknowledged", assignment="unassigned")), ["SO-02", "SO-05"])

    def test_exclusion_survives_a_cold_ack_cache_at_acknowledge_time(self):
        self.zoho.view_lags = True
        self.acknowledge("id2")  # never listed before: ack cache is empty when the acknowledge lands
        self.assertEqual(self.inventory()["total"], 9)

    def test_persists_across_backend_restart(self):
        self.acknowledge("id2", "id5")
        self.restart_backend()  # in-memory caches and overrides gone; Zoho now serves the truth
        page = self.inventory()
        self.assertEqual(page["total"], 8)
        self.assertFalse({"SO-02", "SO-05"} & set(self.numbers(page)))

    def test_override_is_dropped_once_zoho_agrees(self):
        self.acknowledge("id2")
        self.assertIn("id2", load_planning._ack_overrides)
        load_planning._invalidate_ack_cache()
        self.inventory()  # view is current: it now contains id2
        self.assertNotIn("id2", load_planning._ack_overrides)

    def test_partial_failure_only_the_successful_one_leaves(self):
        self.zoho.fail_ack = {"id5"}
        results = self.acknowledge("id2", "id5")
        self.assertTrue(results["id2"]["acknowledged"])
        self.assertIsInstance(results["id5"], Exception)
        page = self.inventory()
        self.assertEqual(page["total"], 9)
        self.assertIn("SO-05", self.numbers(page))
        self.assertNotIn("SO-02", self.numbers(page))

    def test_remove_acknowledge_returns_it_to_inventory(self):
        self.acknowledge("id2", "id5")
        self.assertEqual(self.inventory()["total"], 8)
        load_planning.remove_acknowledge_sales_order_route("id2")
        page = self.inventory()
        self.assertEqual(page["total"], 9)
        self.assertIn("SO-02", self.numbers(page))
        self.assertEqual(self.numbers(self.inventory(status="Acknowledged", assignment="unassigned")), ["SO-05"])

    def test_remove_acknowledge_wins_over_a_lagging_view_and_restart_is_consistent(self):
        self.acknowledge("id2")
        self.inventory()  # view now holds id2
        self.zoho.view_snapshot = {"id2"}
        self.zoho.view_lags = True  # view still reports id2 after the removal
        load_planning.remove_acknowledge_sales_order_route("id2")
        load_planning._invalidate_ack_cache()
        self.assertIn("SO-02", self.numbers(self.inventory()))
        self.zoho.view_lags = False
        self.restart_backend()
        self.assertIn("SO-02", self.numbers(self.inventory()))

    def test_acknowledge_does_not_refetch_the_window(self):
        self.inventory()  # warm window + ack cache
        window_lists, view_calls, details = (self.zoho.calls[k] for k in ("window_list", "view", "detail"))
        self.acknowledge("id2", "id5")
        self.inventory()
        self.assertEqual(self.zoho.calls["window_list"], window_lists)  # no list re-pull
        self.assertEqual(self.zoho.calls["detail"], details)  # no per-order detail re-fetch
        self.assertEqual(self.zoho.calls["view"], view_calls)  # ack set served from cache + overlay
        self.assertEqual(self.zoho.calls["ack_post"], 2)  # exactly one POST per acknowledge

    def test_other_process_excludes_after_window_ttl_even_when_view_lags(self):
        # Process B warmed its own stale confirmed window before process A acknowledged.
        self.inventory()
        self.zoho.view_snapshot = set()
        self.zoho.view_lags = True
        self.acknowledge("id2")
        # Simulate a different worker: it has no local overlay from process A, and its
        # stale window can survive until the 60s TTL expires.
        load_planning._ack_overrides.clear()
        lc.invalidate_windows()
        page = self.inventory()
        self.assertNotIn("SO-02", self.numbers(page))
        self.assertEqual(page["total"], 9)

    def test_acknowledge_directly_in_zoho_is_absent_on_next_uncached_refresh(self):
        self.inventory()
        self.zoho.ack("id2")
        lc.invalidate_windows()
        load_planning._invalidate_ack_cache()
        page = self.inventory()
        self.assertNotIn("SO-02", self.numbers(page))


class AcknowledgedMeansOwnSubStatusTests(ZohoBackedTestCase):
    """An order is acknowledged when its own current_sub_status says cs_acknowl; Zoho's custom view
    and the local overlay are fallbacks."""

    def setUp(self):
        super().setUp()
        self.zoho.view_lags = True  # the Acknowledged custom view is frozen (empty) in these tests
        self.zoho.view_snapshot = set()

    def set_sub(self, order_id, value):
        self.zoho.orders[order_id]["current_sub_status"] = self.zoho.orders[order_id]["order_sub_status"] = value
        self.reset_caches()

    def test_cs_acknowl_not_in_view_goes_to_load_planning_not_inventory(self):
        self.set_sub("id2", "cs_acknowl")
        self.assertNotIn("SO-02", self.numbers(self.inventory()))
        self.assertEqual(self.inventory()["total"], 9)
        self.assertEqual(self.numbers(self.inventory(status="Acknowledged", assignment="unassigned")), ["SO-02"])

    def test_case_insensitive(self):
        self.set_sub("id2", "CS_ACKNOWL")
        self.assertNotIn("SO-02", self.numbers(self.inventory()))

    def test_in_view_but_sub_status_back_to_confirmed_is_inventory(self):
        self.zoho.view_snapshot = {"id2"}  # stale view still lists it
        self.set_sub("id2", "confirmed")
        self.assertIn("SO-02", self.numbers(self.inventory()))
        self.assertEqual(self.numbers(self.inventory(status="Acknowledged", assignment="unassigned")), [])

    def test_order_with_no_sub_status_falls_back_to_the_view(self):
        self.zoho.view_snapshot = {"id2"}
        self.set_sub("id2", "")
        self.assertNotIn("SO-02", self.numbers(self.inventory()))
        self.assertEqual(self.numbers(self.inventory(status="Acknowledged", assignment="unassigned")), ["SO-02"])

    def test_restart_with_cold_cache_is_still_correct(self):
        self.set_sub("id2", "cs_acknowl")
        self.inventory()
        self.restart_backend()
        self.assertEqual(self.inventory()["total"], 9)
        self.assertEqual(self.numbers(self.inventory(status="Acknowledged", assignment="unassigned")), ["SO-02"])

    def test_local_acknowledge_beats_a_list_payload_that_has_not_caught_up(self):
        self.acknowledge("id2")
        self.set_sub("id2", "confirmed")  # Zoho's list still says confirmed (lag); overlay survives the cache reset
        load_planning._set_acknowledged("id2", True)
        self.assertNotIn("SO-02", self.numbers(self.inventory()))

    def test_no_extra_zoho_calls_for_the_sub_status_check(self):
        self.set_sub("id2", "cs_acknowl")
        self.inventory(status="Acknowledged", assignment="unassigned")
        self.assertEqual(self.zoho.calls["window_list"], 1)
        self.assertEqual(self.zoho.calls["view"], 1)


class ManifestCapacityTests(unittest.TestCase):
    def setUp(self):
        self.user = CurrentUser(id=1, email="pau@example.com", role="dispatcher", full_name="Pau", status="active")
        self.profile = SimpleNamespace(plate_no="TRK1", rated_capacity_kg=1000.0, is_reefer=True, driver_id=None)
        result = SimpleNamespace(scalar_one_or_none=lambda: self.profile, scalars=lambda: SimpleNamespace(all=lambda: []))
        self.db = SimpleNamespace(execute=lambda *_: result, add=lambda *_: None, flush=lambda: None, commit=lambda: None, refresh=lambda *_: None)

    def order(self, number, kg, vehicle="TRK1", status="assigned"):
        return SimpleNamespace(id=f"id-{number}", salesorder_number=number, customer_name="C", kg=kg, assignment_status=status, vehicle_id=vehicle, manifest_id=None, raw_json={})

    def confirm(self, *orders, vehicle="TRK1"):
        by_id = {o.id: o for o in orders}
        with ExitStack() as stack:
            stack.enter_context(patch.object(pipeline.live_sales_order_cache, "find_cached", side_effect=lambda oid: by_id.get(oid)))
            stack.enter_context(patch.object(pipeline.live_sales_order_cache, "set_assignment"))
            stack.enter_context(patch.object(pipeline, "calculate_order_weight_kg", side_effect=lambda o: o.kg))
            for name in ("sync_history_row", "invalidate_fleet_cache", "_invalidate_assignment_options_cache"):
                stack.enter_context(patch.object(pipeline, name))
            stack.enter_context(patch.object(pipeline, "send_message", side_effect=lambda *a, **k: asyncio.sleep(0)))
            stack.enter_context(patch.object(pipeline.staff_directory_cache, "first_active", return_value=None))
            body = pipeline.ManifestBody(vehicle_id=vehicle, salesorder_ids=[o.id for o in orders])
            return asyncio.run(pipeline.confirm_manifest(body, self.user, self.db))

    def test_over_capacity_confirms_flagged_and_logged(self):
        orders = [self.order("SO-1", 700.0), self.order("SO-2", 500.0)]
        with self.assertLogs("dispatch_dashboard", level="WARNING") as logs:
            result = self.confirm(*orders)
        self.assertTrue(result["over_capacity"])
        self.assertEqual(result["over_capacity_kg"], 200.0)
        self.assertEqual(result["over_capacity_percent"], 20.0)
        self.assertTrue(all(o.assignment_status == "manifested" for o in orders))
        text = " ".join(logs.output)
        self.assertIn("so_number=SO-1,SO-2", text)
        self.assertIn("truck=TRK1", text)
        self.assertIn("over_kg=200.0", text)

    def test_under_capacity_not_flagged(self):
        result = self.confirm(self.order("SO-3", 400.0))
        self.assertFalse(result["over_capacity"])
        self.assertEqual(result["over_capacity_kg"], 0.0)

    def test_exactly_at_capacity_not_flagged(self):
        self.assertFalse(self.confirm(self.order("SO-4", 1000.0))["over_capacity"])

    def test_unknown_weight_not_flagged_and_not_blocked(self):
        result = self.confirm(self.order("SO-5", 5000.0), self.order("SO-6", None))
        self.assertFalse(result["over_capacity"])
        self.assertIn("manifest", result)

    def test_non_capacity_checks_still_block(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.confirm(self.order("SO-7", 10.0, vehicle="OTHER"))
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(HTTPException) as ctx:
            self.confirm(self.order("SO-8", 10.0, status="unassigned"))
        self.assertEqual(ctx.exception.status_code, 409)


class CapacityWarningTests(unittest.TestCase):
    CAPACITY = 1000.0

    def setUp(self):
        self.user = CurrentUser(id=1, email="pau@example.com", role="dispatcher", full_name="Pau", status="active")
        self.profile = SimpleNamespace(plate_no="TRK1", rated_capacity_kg=self.CAPACITY, is_reefer=True)
        self.db = SimpleNamespace(execute=lambda *_: SimpleNamespace(scalar_one_or_none=lambda: self.profile), commit=lambda: None)

    def order(self, number, kg):
        return SimpleNamespace(id=f"id-{number}", salesorder_number=number, kg=kg, assignment_status="unassigned", expected_shipment_date=date(2026, 10, 6), raw_json={}, vehicle_id=None)

    def loaded(self, kg):
        return SimpleNamespace(id="old", salesorder_number="OLD", kg=kg, assignment_status="assigned", vehicle_id="TRK1", expected_shipment_date=date(2026, 10, 6), raw_json={})

    def assign(self, *orders, existing=()):
        by_id = {o.id: o for o in orders}
        patches = (
            patch.object(assignment.live_sales_order_cache, "find_cached", side_effect=lambda oid: by_id.get(oid)),
            patch.object(assignment.live_sales_order_cache, "get_assigned_snapshot", return_value=list(existing)),
            patch.object(assignment.live_sales_order_cache, "set_assignment"),
            patch.object(assignment, "_weight_if_known", side_effect=lambda o: o.kg),
            patch.object(assignment, "sync_history_row"),
            patch.object(assignment, "invalidate_fleet_cache"),
        )
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            body = assignment.AssignmentBody(salesorder_ids=[o.id for o in orders], vehicle_id="TRK1")
            return assignment.assign_order(orders[0].id, body, self.user, self.db)

    def test_over_capacity_assign_succeeds_and_is_flagged_and_logged(self):
        order = self.order("SO-1", 1250.0)
        with self.assertLogs("assignment_notifications", level="WARNING") as logs:
            result = self.assign(order)
        self.assertTrue(result["success"])
        self.assertEqual(order.assignment_status, "assigned")
        self.assertTrue(result["over_capacity"])
        self.assertEqual(result["over_capacity_kg"], 250.0)
        self.assertEqual(result["over_capacity_percent"], 25.0)
        line = "\n".join(logs.output)
        self.assertIn("so_number=SO-1", line)
        self.assertIn("truck=TRK1", line)
        self.assertIn("over_kg=250.0", line)

    def test_total_assigned_weight_over_capacity_is_flagged(self):
        result = self.assign(self.order("SO-2", 400.0), existing=[self.loaded(800.0)])
        self.assertTrue(result["success"])
        self.assertEqual(result["over_capacity_kg"], 200.0)

    def test_under_capacity_is_not_flagged(self):
        result = self.assign(self.order("SO-3", 400.0), existing=[self.loaded(500.0)])
        self.assertTrue(result["success"])
        self.assertFalse(result["over_capacity"])
        self.assertEqual(result["over_capacity_kg"], 0.0)

    def test_exactly_at_capacity_is_not_flagged(self):
        result = self.assign(self.order("SO-4", 1000.0))
        self.assertTrue(result["success"])
        self.assertFalse(result["over_capacity"])

    def test_unknown_weight_is_not_flagged_and_not_blocked(self):
        result = self.assign(self.order("SO-5", None))
        self.assertTrue(result["success"])
        self.assertFalse(result["over_capacity"])
        self.assertFalse(result["capacity_verified"])
        self.assertIsNotNone(result["capacity_warning"])

    def test_unknown_weight_alongside_known_over_weight_is_not_flagged(self):
        result = self.assign(self.order("SO-6", 5000.0), self.order("SO-7", None))
        self.assertTrue(result["success"])
        self.assertFalse(result["over_capacity"])

    def test_capacity_overage_helper(self):
        self.assertIsNone(assignment.capacity_overage(None, 5000.0))
        self.assertIsNone(assignment.capacity_overage(1000, 1000.0))
        self.assertIsNone(assignment.capacity_overage(1000, 400.0, None))
        self.assertEqual(assignment.capacity_overage(1000, 400.0, 700.0)["over_kg"], 100.0)
        self.assertEqual(assignment.capacity_overage(1000, 400.0, 700.0)["over_percent"], 10.0)


if __name__ == "__main__":
    unittest.main()
