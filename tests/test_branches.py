"""Zoho branches: all-branch default, Branch filter, badges/counts, number search for every prefix,
Acknowledged-view independence for SSI, per-branch stock mapping. Zoho is faked in memory."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET", "test")

from routers import load_planning
from services import branches as bs
from services import live_sales_order_cache as lc
from services import warehouse_stock as ws
from tests.test_inventory_ack_hold_capacity import SHIP, ZohoBackedTestCase

RGF, MSSI, SSI, RC, RFS = (b["id"] for b in bs.BRANCHES)
UNKNOWN = "999000111"


def order(oid, number, branch_id, branch_name, sub="confirmed"):
    return {"salesorder_id": oid, "salesorder_number": number, "customer_name": f"Cust {oid}", "status": "confirmed", "current_sub_status": sub, "order_sub_status": sub,
            "shipment_date": SHIP, "last_modified_time": "t0", "branch_id": branch_id, "branch_name": branch_name, "line_items": []}


class BranchTestCase(ZohoBackedTestCase):
    COUNT = 0

    def setUp(self):
        super().setUp()
        self.zoho.orders = {o["salesorder_id"]: o for o in (
            order("r1", "SO26-18001", RGF, "Rare Global Food Trading Corp."),
            order("r2", "SO26-18002", RGF, "Rare Global Food Trading Corp."),
            order("w1", "WM-SO26-00247", RGF, "Rare Global Food Trading Corp."),
            order("m1", "MS-SO-02016", MSSI, "Meat and Seafood Specialist Inc."),
            order("m2", "MS-SO-02017", MSSI, "Meat and Seafood Specialist Inc."),
            order("s1", "SS SO26-16612", SSI, "SariSuki Store Inc."),
            order("s2", "SS SO26-16613", SSI, "SariSuki Store Inc.", sub="cs_acknowl"),
            order("u1", "XX-1", UNKNOWN, "Foo Store"),
        )}
        # Zoho's Acknowledged custom view only covers RGF + MSSI (rule: branch = RGF OR MSSI), never SSI.
        self.zoho.view = lambda customview_id, page=1, per_page=200: {"salesorders": [{"salesorder_id": i} for i, o in sorted(self.zoho.orders.items()) if o["current_sub_status"] == "cs_acknowl" and o["branch_id"] in (RGF, MSSI)], "page_context": {"has_more_page": False}}
        patcher = patch.object(load_planning, "fetch_sales_orders_by_customview", self.zoho.view)
        patcher.start()
        self.addCleanup(patcher.stop)

    def listing(self, branches=None, status="All except acknowledged", assignment=None, search=None):
        return load_planning.list_sales_orders(date_from=SHIP, date_to=SHIP, status=status, search=search, assignment=assignment, cities=None, vehicle=None, customer=None, delivery_status=None, branches=branches, page=1, per_page=100, db=SimpleNamespace())


class AllBranchesByDefaultTests(BranchTestCase):
    def test_default_includes_every_branch(self):
        page = self.listing()
        self.assertEqual(self.numbers(page), sorted(["SO26-18001", "SO26-18002", "WM-SO26-00247", "MS-SO-02016", "MS-SO-02017", "SS SO26-16612", "XX-1"]))
        self.assertEqual(page["total"], 7)

    def test_ms_orders_visible_with_badge(self):
        items = {i["salesorder_number"]: i for i in self.listing()["items"]}
        for number in ("MS-SO-02016", "MS-SO-02017"):
            self.assertEqual((items[number]["branch_code"], items[number]["branch_id"]), ("MSSI", MSSI))
        self.assertEqual(items["SS SO26-16612"]["branch_code"], "SSI")

    def test_badge_comes_from_branch_not_prefix(self):
        items = {i["salesorder_number"]: i for i in self.listing()["items"]}
        self.assertEqual(items["WM-SO26-00247"]["branch_code"], "RGF")  # WM-SO is an RGF series
        self.assertEqual(items["XX-1"]["branch_code"], "FS")  # unknown branch: initials of its Zoho name
        self.assertEqual(items["XX-1"]["branch_name"], "Foo Store")

    def test_options_always_list_all_five_plus_unknown_seen(self):
        ids = [b["id"] for b in self.listing()["branches"]]
        self.assertEqual(ids[:5], [RGF, MSSI, SSI, RC, RFS])
        self.assertIn(UNKNOWN, ids)
        # even when the window has no Rare Cuts / Rare Food Shop orders, the config still offers them
        self.assertEqual([b["id"] for b in load_planning.list_branches()["branches"]], [RGF, MSSI, SSI, RC, RFS])


class BranchFilterTests(BranchTestCase):
    def test_single_branch(self):
        page = self.listing(branches=MSSI)
        self.assertEqual(self.numbers(page), ["MS-SO-02016", "MS-SO-02017"])
        self.assertEqual(page["total"], 2)

    def test_multi_select(self):
        page = self.listing(branches=f"{MSSI},{SSI}")
        self.assertEqual(self.numbers(page), ["MS-SO-02016", "MS-SO-02017", "SS SO26-16612"])

    def test_branch_with_no_orders_is_empty(self):
        self.assertEqual(self.listing(branches=RC)["total"], 0)

    def test_counts_per_branch_ignore_the_branch_filter(self):
        expected = {RGF: 3, MSSI: 2, SSI: 1, UNKNOWN: 1}
        self.assertEqual(self.listing()["branch_counts"], expected)
        self.assertEqual(self.listing(branches=MSSI)["branch_counts"], expected)  # options keep their counts while filtered

    def test_filter_applies_to_load_planning_and_exports_rows(self):
        rows = load_planning._filtered_rows(SimpleNamespace(), SHIP, SHIP, None, None, "unassigned", branches=SSI)
        self.assertEqual({r.salesorder_number for r in rows}, {"SS SO26-16612", "SS SO26-16613"})
        export_rows = load_planning._filtered_rows(SimpleNamespace(), SHIP, SHIP, "All except acknowledged", None, None, branches=MSSI)
        self.assertEqual({r.salesorder_number for r in export_rows}, {"MS-SO-02016", "MS-SO-02017"})

    def test_email_context_uses_the_filter(self):
        ctx = load_planning.EmailFilterContext(date_from=SHIP, date_to=SHIP, status="All except acknowledged", branches=MSSI)
        with patch.object(load_planning, "_hydrate_export_rows", lambda db, rows: rows):
            rows = load_planning._email_rows(SimpleNamespace(), ctx)
        self.assertEqual({r.salesorder_number for r in rows}, {"MS-SO-02016", "MS-SO-02017"})

    def test_bulk_acknowledge_respects_branch_filter(self):
        result = load_planning.acknowledge_filtered_sales_orders(date_from=SHIP, date_to=SHIP, status="All except acknowledged", search=None, branches=MSSI, db=SimpleNamespace(), current_user=self.user)
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(self.zoho.acked() & {"r1", "r2", "w1", "s1", "u1"}, set())
        self.assertTrue({"m1", "m2"} <= self.zoho.acked())


class NumberSearchTests(BranchTestCase):
    def found(self, text):
        return self.numbers(self.listing(search=text))

    def test_every_prefix_format(self):
        self.assertEqual(self.found("MS-SO-02016"), ["MS-SO-02016"])
        self.assertEqual(self.found("SS SO26-16612"), ["SS SO26-16612"])  # with the space
        self.assertEqual(self.found("WM-SO26-00247"), ["WM-SO26-00247"])
        self.assertEqual(self.found("SO26-18001"), ["SO26-18001"])

    def test_punctuation_and_case_insensitive(self):
        self.assertEqual(self.found("ss-so26-16612"), ["SS SO26-16612"])
        self.assertEqual(self.found("SSSO2616612"), ["SS SO26-16612"])
        self.assertEqual(self.found("ms so 02017"), ["MS-SO-02017"])

    def test_partial_prefix(self):
        self.assertEqual(self.found("MS-SO"), ["MS-SO-02016", "MS-SO-02017"])
        self.assertEqual(self.found("SS SO26"), ["SS SO26-16612"])


class AcknowledgedViewExcludesSsiTests(BranchTestCase):
    def test_view_does_not_contain_the_ssi_order(self):
        self.assertNotIn("s2", load_planning._fetch_acknowledged_ids_from_zoho())

    def test_ssi_cs_acknowl_is_acknowledged_without_the_view(self):
        view_ids = load_planning._fetch_acknowledged_ids_from_zoho()
        row = next(r for r in load_planning._filtered_rows(SimpleNamespace(), SHIP, SHIP, None, None, "unassigned") if r.id == "s2")
        self.assertTrue(load_planning.row_is_acknowledged(row, view_ids))
        self.assertTrue(load_planning._is_acknowledged(row))

    def test_ssi_acknowledged_order_is_in_load_planning_and_out_of_inventory(self):
        load_planning_page = self.listing(status="Acknowledged", assignment="unassigned")
        self.assertEqual(self.numbers(load_planning_page), ["SS SO26-16613"])
        self.assertNotIn("SS SO26-16613", self.numbers(self.listing()))

    def test_all_branch_acknowledge_path_and_substatus_codes(self):
        out = self.acknowledge("m1", "s1")
        self.assertTrue(out["m1"]["acknowledged"] and out["s1"]["acknowledged"])
        self.assertEqual(self.zoho.orders["s1"]["current_sub_status"], "cs_acknowl")
        # SSI order already acknowledged -> retry path, no new Zoho acknowledge
        before = self.zoho.calls["ack_post"]
        again = self.acknowledge("s2")
        self.assertTrue(again["s2"]["already_acknowledged"])
        self.assertEqual(self.zoho.calls["ack_post"], before)

    def test_on_hold_codes_apply_to_every_branch(self):
        for sub in ("cs_onhold", "cs_onholds"):
            self.assertTrue(lc.is_on_hold({"branch_id": SSI, "current_sub_status": sub}))


class BranchConfigTests(unittest.TestCase):
    def test_branch_of_ignores_number_prefix(self):
        self.assertIsNone(bs.branch_of({"salesorder_number": "MS-SO-02016"}))  # no branch fields -> no badge
        self.assertEqual(bs.branch_of({"branch_id": SSI, "salesorder_number": "SO-1"})["code"], "SSI")
        self.assertEqual(bs.branch_of(SimpleNamespace(raw_json={"branch_id": MSSI}))["code"], "MSSI")
        self.assertIsNone(bs.branch_of(SimpleNamespace(raw_json=None)))  # past-dated Neon row

    def test_parse_ids(self):
        self.assertEqual(bs.parse_ids(f" {RGF}, ,{MSSI} "), {RGF, MSSI})
        self.assertEqual(bs.parse_ids(None), set())
        self.assertEqual(bs.parse_ids(object()), set())


WAREHOUSES = {"warehouses": [
    {"warehouse_name": "Mets Cold Storage Services Inc. RGF", "warehouse_available_for_sale_stock": 10},
    {"warehouse_name": "Glacier South RGF", "warehouse_available_for_sale_stock": 20},
    {"warehouse_name": "Chilled - Mets Cold Storage Services Inc. RGF", "warehouse_available_for_sale_stock": 99},
    {"warehouse_name": "METS RGF (Near-Expiry Items / Production Reserves)", "warehouse_available_for_sale_stock": 98},
    {"warehouse_name": "Mets Cold Storage Services Inc. MSSI / SUPERMARKET", "warehouse_available_for_sale_stock": 30},
    {"warehouse_name": "Glacier South MSSI", "warehouse_available_for_sale_stock": 40},
    {"warehouse_name": "SariSuki Store Inc. Warehouse", "warehouse_available_for_sale_stock": 50},
    {"warehouse_name": "Glacier South MSSI(DEACTIVATED)", "warehouse_available_for_sale_stock": 77},
]}


class StockMappingTests(unittest.TestCase):
    def parse(self, branch_id, enabled):
        with patch.dict(os.environ, {"BRANCH_STOCK_MAPPING": "1" if enabled else ""}):
            return ws._parse_stock(WAREHOUSES, "i1", branch_id)

    def test_flag_off_is_the_unchanged_legacy_logic_for_every_branch(self):
        # Legacy quirk, deliberately untouched while the flag is off: the "MSSI / SUPERMARKET" warehouse
        # is not excluded by the "for supermarket" rule, so it is also read as "Mets" and, being listed
        # after the RGF one here, wins. BRANCH_STOCK_MAPPING=1 fixes this per branch.
        for branch in (None, RGF, MSSI, SSI):
            self.assertEqual(self.parse(branch, False), {"mets": 30, "glacier": 20})

    def test_rgf(self):
        self.assertEqual(self.parse(RGF, True), {"mets": 10, "glacier": 20})

    def test_mssi(self):
        self.assertEqual(self.parse(MSSI, True), {"mets": 30, "glacier": 40})

    def test_ssi_is_its_own_warehouse(self):
        self.assertEqual(self.parse(SSI, True), {"mets": None, "glacier": None, "other": 50, "other_name": "SariSuki Store Inc. Warehouse"})

    def test_unconfigured_or_missing_branch_keeps_default(self):
        self.assertEqual(self.parse(None, True), self.parse(None, False))
        self.assertEqual(self.parse("123", True), self.parse(None, False))


class ExportBranchColumnTests(unittest.TestCase):
    ORDER = SimpleNamespace(expected_shipment_date=None, salesorder_number="SS SO26-1", customer_name="C", shipping_address=None, raw_json={"branch_id": SSI, "line_items": [{"item_id": "i1", "name": "Egg", "quantity": 1, "unit": "case"}]})

    def rows(self, stock):
        from services.inventory_exports import flatten_confirmed_order
        return flatten_confirmed_order(self.ORDER, lambda item_id: stock, lambda order: ("", ""))

    def test_headers_and_rows_line_up(self):
        from services.inventory_exports import CONFIRMED_HEADERS, CONFIRMED_WIDTHS
        row = self.rows({"mets": None, "glacier": None, "other": 50.0, "other_name": "SariSuki Store Inc. Warehouse"})[0]
        self.assertEqual(len(row), len(CONFIRMED_HEADERS))
        self.assertEqual(len(CONFIRMED_WIDTHS), len(CONFIRMED_HEADERS))
        self.assertEqual(row[CONFIRMED_HEADERS.index("Branch Warehouse (Qty Available for Sale)")], "50 (SariSuki Store Inc. Warehouse)")

    def test_branch_without_own_warehouse_is_blank_and_negative_is_kept(self):
        from services.inventory_exports import CONFIRMED_HEADERS
        col = CONFIRMED_HEADERS.index("Branch Warehouse (Qty Available for Sale)")
        self.assertEqual(self.rows({"mets": 1, "glacier": 2})[0][col], "")
        self.assertEqual(self.rows({"other": -12.5, "other_name": "W"})[0][col], "-12.50 (W)")

    def test_excel_and_pdf_build_with_negative_branch_stock(self):
        from openpyxl import load_workbook
        from services.inventory_exports import make_excel, make_pdf
        rows = self.rows({"mets": -3.0, "glacier": 4.0, "other": -2000.0, "other_name": "SariSuki Store Inc. Warehouse"})
        sheet = load_workbook(make_excel(rows, "cap", "confirmed")).active
        self.assertEqual(sheet.cell(1, 13).value, "Branch Warehouse (Qty Available for Sale)")
        self.assertEqual(sheet.cell(2, 11).value, -3.0)  # Mets stays negative, not clamped
        self.assertEqual(sheet.cell(2, 13).value, "-2,000 (SariSuki Store Inc. Warehouse)")
        self.assertEqual(sheet.cell(2, 13).font.color.rgb[-6:], "C00000")
        self.assertTrue(make_pdf(rows, "cap", "confirmed").getvalue().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
