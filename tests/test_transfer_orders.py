"""Transfer orders in Load Planning: the Draft Mets<->Glacier filter (by warehouse ID), caching / Hard refresh, kg logic, and the combined
SO + TO assign / unassign / bulk-unassign flow. Mocked only: no Zoho, no database."""
from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace

import pytest

os.environ.setdefault("JWT_SECRET", "test")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost")

from auth.dependencies import CurrentUser  # noqa: E402
from routers import assignment, load_planning  # noqa: E402
from services import assignment_email_status, item_weight, live_sales_order_cache as lc, transfer_orders  # noqa: E402

METS = "4489499000064563124"
GLACIER = "4489499000064711027"
MSSI_METS = "4489499000066605796"
PRODUCTION = "4489499000099999999"
DAY = "2026-10-14"


def record(tid, number, source, dest, *, status="draft", day=DAY, quantity=10, source_name=None, dest_name=None):
    return {
        "transfer_order_id": tid, "transfer_order_number": number, "date": day, "status": status, "quantity_transfer": quantity,
        "from_warehouse_id": source, "to_warehouse_id": dest, "from_warehouse_name": source_name or "x", "to_warehouse_name": dest_name or "y",
        "reason": "Restock", "created_by_name": "Ana", "last_modified_by_name": "Ben", "last_modified_time": "2026-10-13T08:00:00+0800",
    }


RECORDS = [
    record("1", "TO-00001", METS, GLACIER, source_name="Mets Cold Storage", dest_name="Glacier Cold Storage"),
    record("2", "TO-00002", GLACIER, METS, source_name="Glacier Cold Storage", dest_name="Mets Cold Storage"),
    record("3", "TO-00003", METS, PRODUCTION, dest_name="Production Area RC"),
    record("4", "TO-00004", PRODUCTION, GLACIER, source_name="Production Area RC"),
    record("5", "TO-00005", METS, GLACIER, status="transferred"),
    record("6", "TO-00006", METS, GLACIER, status="in_transit"),
    record("7", "TO-00007", MSSI_METS, GLACIER, day="2026-10-15"),  # the MSSI Mets warehouse is Mets too
    record("8", "TO-00008", "no-id-but-named-mets", GLACIER, source_name="Mets Cold Storage"),  # unknown ID: never matched by name
]
DETAILS = {
    "1": {"transfer_order_id": "1", "line_items": [{"item_id": "i-kg", "name": "Pork belly", "sku": "PB", "quantity_transfer": 12, "unit": "kg"}, {"item_id": "i-box", "name": "Chicken", "sku": "CH", "quantity_transfer": 4, "unit": "box"}]},
    "2": {"transfer_order_id": "2", "line_items": [{"item_id": "i-kg", "name": "Pork belly", "sku": "PB", "quantity_transfer": 5, "unit": "kg"}]},
    "7": {"transfer_order_id": "7", "line_items": [{"item_id": "i-unknown", "name": "Mystery", "sku": "MY", "quantity_transfer": 3, "unit": "pcs"}]},
}


class InlinePool:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class Zoho:
    def __init__(self):
        self.list_calls = 0
        self.detail_calls: list[str] = []


@pytest.fixture
def zoho(monkeypatch):
    transfer_orders.reset()
    fake = Zoho()

    def page(page=1, per_page=200):
        fake.list_calls += 1
        return {"transferorders": [dict(r) for r in RECORDS], "page_context": {"has_more_page": False}}

    def detail(tid):
        fake.detail_calls.append(tid)
        listed = next((dict(r) for r in RECORDS if r["transfer_order_id"] == tid), {})  # Zoho's detail carries the header fields too
        return {"code": 0, "transfer_order": {**listed, **DETAILS.get(tid, {"line_items": []}), "transfer_order_id": tid}}

    monkeypatch.setattr(transfer_orders, "fetch_transfer_orders_page", page)
    monkeypatch.setattr(transfer_orders, "fetch_transfer_order_detail", detail)
    monkeypatch.setattr(transfer_orders, "_pool", InlinePool())
    monkeypatch.setitem(item_weight._cache, "i-box", 2.5)  # package weight: 2.5 kg per box
    yield fake
    transfer_orders.reset()
    with lc._state_lock:
        for oid in [o for o in lc._assignment_state if str(o).startswith("TO:") or str(o).startswith("so-")]:
            lc._assignment_state.pop(oid, None)
    lc._assigned_zoho.clear()


# ---- the filter ---------------------------------------------------------------------------------------------------------------


def test_only_draft_mets_glacier_both_directions_by_warehouse_id(zoho):
    records, _ = transfer_orders.get_records()
    assert sorted(records) == ["1", "2", "7"]  # 3/4 other warehouse, 5/6 not Draft, 8 named Mets but its ID is unknown


def test_route_sides_use_ids_never_names():
    assert transfer_orders.warehouse_side(METS) == "mets" and transfer_orders.warehouse_side(MSSI_METS) == "mets"
    assert transfer_orders.warehouse_side(GLACIER) == "glacier"
    assert transfer_orders.warehouse_side("Mets Cold Storage") is None
    assert transfer_orders.warehouse_side(PRODUCTION) is None


def test_date_filter_is_the_expected_delivery_date(zoho):
    rows, _, _ = transfer_orders.list_rows(date(2026, 10, 14), date(2026, 10, 14))
    assert [r.salesorder_number for r in rows] == ["TO-00001", "TO-00002"]
    rows, _, _ = transfer_orders.list_rows(date(2026, 10, 14), date(2026, 10, 15))
    assert len(rows) == 3
    assert transfer_orders.list_rows(date(2026, 10, 20), date(2026, 10, 20))[0] == []


def test_list_is_cached_and_hard_refresh_bypasses_the_cache(zoho):
    transfer_orders.list_rows(date(2026, 10, 14), date(2026, 10, 14))
    transfer_orders.list_rows(date(2026, 10, 15), date(2026, 10, 15))  # another date: still the cached list, no new Zoho call
    assert zoho.list_calls == 1
    _, refreshed_a = transfer_orders.get_records()
    _, refreshed_b = transfer_orders.get_records(force=True)  # Hard refresh
    assert zoho.list_calls == 2 and refreshed_b >= refreshed_a


def test_line_details_are_fetched_once_and_only_for_shown_orders(zoho):
    transfer_orders.list_rows(date(2026, 10, 14), date(2026, 10, 14))
    transfer_orders.list_rows(date(2026, 10, 14), date(2026, 10, 14))
    assert sorted(zoho.detail_calls) == ["1", "2"]  # TO 7 is on another day: no detail call; none repeated


# ---- kg ----------------------------------------------------------------------------------------------------------------------


def test_quantity_in_kg_is_used_as_is_and_other_units_use_the_item_package_weight(zoho):
    transfer_orders.list_rows(date(2026, 10, 14), date(2026, 10, 14))
    rows, _, pending = transfer_orders.list_rows(date(2026, 10, 14), date(2026, 10, 14))
    assert pending == 0
    first = transfer_orders.summary(rows[0])
    assert [p["total_weight_kg"] for p in first["products"]] == [12.0, 10.0]  # 12 kg as is; 4 boxes x 2.5 kg
    total, complete, missing = transfer_orders.total_weight_kg(rows)
    assert (total, complete, missing) == (27.0, True, [])  # 22 + 5


def test_kg_plus_while_lines_or_package_weights_are_unknown(zoho):
    rows, _, pending = transfer_orders.list_rows(date(2026, 10, 14), date(2026, 10, 15))
    assert pending == 3  # first load: no lines yet
    rows, _, pending = transfer_orders.list_rows(date(2026, 10, 14), date(2026, 10, 15))
    total, complete, missing = transfer_orders.total_weight_kg(rows)
    assert pending == 0 and complete is False and missing == ["i-unknown"]  # TO 7's pcs item has no package weight
    assert total == 27.0


def test_lines_not_loaded_is_never_reported_as_zero_kg(zoho, monkeypatch):
    monkeypatch.setattr(transfer_orders, "request_details", lambda records: len(records))  # fill never finishes
    rows, _, _ = transfer_orders.list_rows(date(2026, 10, 14), date(2026, 10, 14))
    assert transfer_orders.total_weight_kg(rows)[:2] == (0.0, False)


# ---- the list endpoint -------------------------------------------------------------------------------------------------------


def call_list(**overrides):
    args = dict(date_from=DAY, date_to=DAY, search=None, assignment=None, page=1, per_page=25, db=None)
    args.update(overrides)
    return load_planning.list_transfer_orders(**args)


def test_endpoint_shape_columns_and_no_acknowledge_fields(zoho):
    call_list()
    body = call_list()
    assert body["total"] == 2 and body["weight_complete"] is True and body["total_weight_kg"] == 27.0 and body["source"] == "zoho"
    assert body["last_refreshed_at"]
    first = body["items"][0]
    assert first["order_type"] == "TO" and first["id"] == "TO:1" and first["transfer_order_number"] == "TO-00001"
    assert (first["source_warehouse"], first["destination_warehouse"]) == ("Mets Cold Storage", "Glacier Cold Storage")
    assert (first["reason"], first["order_status"], first["created_by"], first["last_modified_by"]) == ("Restock", "draft", "Ana", "Ben")
    assert first["expected_shipment_date"] == DAY and first["total_item_quantity"] == 16.0
    assert first["zoho_lock"] is None and first["branch_id"] is None


def test_endpoint_search_and_default_dates(zoho, monkeypatch):
    assert call_list(search="glacier cold")["total"] == 1 or call_list(search="TO-00002")["total"] == 1
    assert call_list(search="to00001")["items"][0]["id"] == "TO:1"
    monkeypatch.setattr(load_planning, "datetime", SimpleNamespace(now=lambda tz=None: SimpleNamespace(date=lambda: date(2026, 10, 14))))
    assert call_list(date_from=None, date_to=None)["total"] == 2  # the default is today, exactly like the SO list


def test_hard_refresh_endpoint_calls_zoho_once_more(zoho):
    call_list()
    before = zoho.list_calls
    result = load_planning.hard_refresh_transfer_orders()
    assert zoho.list_calls == before + 1 and result["count"] == 3 and result["refreshed_at"]


# ---- combined assign / unassign ----------------------------------------------------------------------------------------------


class Flow:
    def __init__(self, monkeypatch):
        self.user = CurrentUser(id=1, email="pau@example.com", role="dispatcher", full_name="Pau", status="active")
        profile = SimpleNamespace(plate_no="TRK1", rated_capacity_kg=1000, is_reefer=None, is_third_party=False, capacity_note=None)
        self.db = SimpleNamespace(execute=lambda stmt: SimpleNamespace(scalar_one_or_none=lambda: profile), commit=lambda: None)
        self.so = SimpleNamespace(id="so-1", salesorder_number="SO-1", customer_name="Customer 1", shipping_address=None, order_status="confirmed", manifest_id=None, route_id=None,
                                  raw_json={"shipping_address": {"city": "Manila"}, "line_items": [{"quantity": 7, "unit": "kg"}]}, assignment_status="unassigned",
                                  expected_shipment_date=date(2026, 10, 14), vehicle_id=None, driver_id=None, helper_ids=[], assigned_at=None, assigned_by=None, order_type="SO")
        lc._assigned_zoho["so-1"] = self.so
        monkeypatch.setattr(assignment.staff_directory_cache, "get_by_id", lambda i: {"id": i, "name": "Juan"})
        monkeypatch.setattr(assignment, "sync_history_row", lambda db, o: None)
        monkeypatch.setattr(assignment, "invalidate_fleet_cache", lambda: None)
        monkeypatch.setattr(lc, "get_assigned_snapshot", lambda: list(lc._assigned_zoho.values()))
        monkeypatch.setattr(lc, "invalidate_assigned_zoho", lambda: None)  # keep the rows the test holds
        monkeypatch.setattr(lc, "invalidate_windows", lambda: None)

    def assign(self, ids):
        body = assignment.AssignmentBody(salesorder_ids=ids, vehicle_id="TRK1", driver_ids=[1])
        return assignment.assign_order(ids[0], body, self.user, self.db)


@pytest.fixture
def flow(zoho, monkeypatch):
    return Flow(monkeypatch)


def test_assign_mixed_so_and_to_on_one_truck_then_unassign_restores_the_to_tab(zoho, flow):
    assert call_list()["total"] == 2
    result = flow.assign(["TO:1", "so-1"])
    assert result["success"] is True and sorted(result["salesorder_ids"]) == ["TO:1", "so-1"]
    batch = result["assignment_batch_id"]
    assert sorted(lc.ids_for_batch(batch)) == ["TO:1", "so-1"]  # one batch: a mixed load
    # gone from the TO tab at once (and so from the Load Planning unassigned list, which reads the same list)
    assert [i["id"] for i in call_list()["items"]] == ["TO:2"]
    assert call_list(assignment="unassigned")["total"] == 1
    # unassign puts it back
    assignment.unassign_order("TO:1", flow.user, flow.db)
    assert sorted(i["id"] for i in call_list()["items"]) == ["TO:1", "TO:2"]


def test_cannot_assign_a_to_that_is_not_a_draft_mets_glacier_order(zoho, flow):
    with pytest.raises(Exception) as caught:
        flow.assign(["TO:3"])
    assert getattr(caught.value, "status_code", None) in (404, 409)
    assert lc.get_assignment("TO:3") is None


def test_bulk_unassign_returns_orders_counts_by_type_and_clears_email_status(zoho, flow):
    result = flow.assign(["TO:1", "TO:2", "so-1"])
    batch = result["assignment_batch_id"]
    assignment_email_status.record(["TO:1", "TO:2", "so-1"], "sent", message_id="m1")
    assert assignment_email_status.summarize(["TO:1"])["status"] == "sent"
    out = assignment.bulk_unassign(assignment.BulkUnassignBody(order_ids=["TO:1", "so-1", "TO:9999-not-assigned"]), flow.user, flow.db)
    assert sorted(out["unassigned"]) == ["TO:1", "so-1"] and out["count"] == 2 and out["count_by_type"] == {"SO": 1, "TO": 1}
    assert [s["id"] for s in out["skipped"]] == ["TO:9999-not-assigned"]
    assert assignment_email_status.summarize(["TO:1", "so-1"])["status"] is None  # cleared
    assert (lc.get_assignment("TO:1") or {}).get("assignment_batch_id") is None
    assert assignment_email_status.summarize(["TO:2"])["status"] == "not_sent"  # still on the batch: its email no longer matches the load
    assert lc.ids_for_batch(batch) == ["TO:2"]
    assert sorted(i["id"] for i in call_list()["items"]) == ["TO:1"]  # TO 1 is back in the TO tab; TO 2 is still assigned
    assert flow.so.assignment_status == "unassigned"


def test_bulk_unassign_validates_the_selection(zoho, flow):
    with pytest.raises(Exception) as empty:
        assignment.bulk_unassign(assignment.BulkUnassignBody(order_ids=[]), flow.user, flow.db)
    assert empty.value.status_code == 400
    with pytest.raises(Exception) as too_many:
        assignment.bulk_unassign(assignment.BulkUnassignBody(order_ids=[f"TO:{i}" for i in range(300)]), flow.user, flow.db)
    assert too_many.value.status_code == 400


def test_confirmed_orders_lists_assigned_to_next_to_so(zoho, flow, monkeypatch):
    flow.assign(["TO:1", "so-1"])
    monkeypatch.setattr(load_planning, "_ack_snapshot", lambda: (set(), {}))
    monkeypatch.setattr(lc, "get_assigned_snapshot_ex", lambda block=True, **kw: (list(lc._assigned_zoho.values()), False))
    monkeypatch.setattr(load_planning, "stock_for_orders_cached", lambda rows: ({}, 0))
    monkeypatch.setattr(load_planning, "_summary", lambda row, db=None, allow_fetch=True: {"id": row.id, "order_type": transfer_orders.order_type_of(row)})

    def rows(types):
        return load_planning._rows_any_branch(None, DAY, DAY, None, None, "assigned", order_types=types)

    assert [r.id for r in rows(None)] == ["so-1"]  # default: SO only (unchanged)
    assert sorted(r.id for r in rows("SO,TO")) == ["TO:1", "so-1"]
    assert [r.id for r in rows("TO")] == ["TO:1"]


def test_driver_checkout_needs_no_invoice_for_a_to(zoho):
    from services import form_templates

    template = {"items": [
        {"key": "invoice_dr_number", "label": "Invoice / DR number", "item_type": "numeric_text", "required": True, "options": None, "prefill": None, "show_when": None},
        {"key": "notes", "label": "Notes", "item_type": "text", "required": False, "options": None, "prefill": None, "show_when": None},
    ]}
    answers, errors = form_templates.validate_checkout(template, {}, False)
    assert [e["key"] for e in errors] == ["invoice_dr_number"]  # an SO still needs it
    answers, errors = form_templates.validate_checkout(template, {}, False, skip_keys=frozenset({"invoice_dr_number"}))
    assert errors == [] and answers == []
