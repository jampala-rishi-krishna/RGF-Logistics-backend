"""Unassign guard (Zoho fulfilled / shipped / partially delivered / delivered, stop checked out; automatic bypass) and the
customer-pickup assign block. Mocked only: no Zoho, no database."""
from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET", "test")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost")

from auth.dependencies import CurrentUser  # noqa: E402
from routers import assignment, load_planning  # noqa: E402
from services import dispatch_changes, live_sales_order_cache as lc, trip_state, unassign_guard  # noqa: E402

DAY = date(2026, 10, 21)


def so(oid, *, status="confirmed", raw=None, assigned=True, batch="b1", delivery_method=None):
    return SimpleNamespace(
        id=oid, salesorder_number=f"SO26-{oid}", customer_name=f"Customer {oid}", shipping_address=None, order_status=status, manifest_id=None, route_id=None,
        raw_json={"status": status, "line_items": [{"quantity": 5, "unit": "kg"}], **(raw or {})}, assignment_status="assigned" if assigned else "unassigned",
        expected_shipment_date=DAY, vehicle_id="TRK1" if assigned else None, driver_id=1 if assigned else None, helper_ids=[1] if assigned else [],
        assigned_at=None, assigned_by=None, assignment_batch_id=batch if assigned else None, order_type="SO", delivery_method=delivery_method,
    )


class World:
    def __init__(self, monkeypatch):
        self.user = CurrentUser(id=1, email="pau@example.com", role="admin", full_name="Pau", status="active")
        profile = SimpleNamespace(plate_no="TRK1", rated_capacity_kg=1000, is_reefer=None, is_third_party=False, capacity_note=None)
        self.added = []
        self.db = SimpleNamespace(execute=lambda stmt: SimpleNamespace(scalar_one_or_none=lambda: profile, scalars=lambda: SimpleNamespace(all=lambda: []), all=lambda: []),
                                  commit=lambda: None, add=self.added.append)
        self.stops: dict[str, str] = {}  # so id -> stop status on trip of batch b1
        monkeypatch.setattr(assignment.staff_directory_cache, "get_by_id", lambda i: {"id": i, "name": "Juan"})
        monkeypatch.setattr(assignment, "sync_history_row", lambda db, o: None)
        monkeypatch.setattr(assignment, "invalidate_fleet_cache", lambda: None)
        monkeypatch.setattr(assignment, "_record_unassign", lambda db, user, row: None)
        monkeypatch.setattr(lc, "get_assigned_snapshot", lambda: list(lc._assigned_zoho.values()))
        monkeypatch.setattr(lc, "invalidate_assigned_zoho", lambda: None)
        monkeypatch.setattr(lc, "invalidate_windows", lambda: None)
        monkeypatch.setattr(lc, "ensure_zoho_data", lambda oid: None)
        monkeypatch.setattr(dispatch_changes.trip_state, "load", self._load)
        monkeypatch.setattr(assignment.assignment_email_status, "clear", lambda ids: None)
        monkeypatch.setattr(assignment.assignment_email_status, "record", lambda *a, **k: None)

    def _load(self, db, batch_ids, names=True):
        data = trip_state.TripData()
        if "b1" in batch_ids:
            trip = SimpleNamespace(id=7, batch_id="b1", status="started", started_at=None)
            data.trips["b1"] = trip
            for oid, status in self.stops.items():
                data.stops[7][oid] = SimpleNamespace(status=status, checkin_at=None, checkout_at=None)
        return data

    def add(self, *orders):
        for order in orders:
            lc._assigned_zoho[order.id] = order
            if order.assignment_status == "assigned":
                lc.set_assignment(order.id, assignment_batch_id=order.assignment_batch_id, assignment_status="assigned")
        return orders

    def assign(self, ids):
        body = assignment.AssignmentBody(salesorder_ids=ids, vehicle_id="TRK1", driver_ids=[1])
        return assignment.assign_order(ids[0], body, self.user, self.db)


@pytest.fixture
def world(monkeypatch):
    w = World(monkeypatch)
    yield w
    with lc._state_lock:
        for oid in [o for o in lc._assignment_state if str(o).startswith("g-")]:
            lc._assignment_state.pop(oid, None)
    for oid in [o for o in lc._assigned_zoho if str(o).startswith("g-")]:
        lc._assigned_zoho.pop(oid, None)


BLOCKED_ZOHO = [
    ({"status": "fulfilled"}, "fulfilled"),
    ({"shipped_status": "shipped"}, "shipped"),
    ({"shipped_status": "partially_shipped"}, "partially shipped"),
    ({"status": "partially_delivered"}, "partially delivered"),
    ({"delivered_status": "delivered"}, "delivered"),
]


@pytest.mark.parametrize("raw,label", BLOCKED_ZOHO)
def test_zoho_status_hides_and_rejects_manual_unassign(world, raw, label):
    (order,) = world.add(so("g-1", raw=raw))
    assert label in unassign_guard.unassign_blocked_reason(order) or "delivered" in unassign_guard.unassign_blocked_reason(order).lower()
    with pytest.raises(HTTPException) as caught:
        assignment.unassign_order("g-1", world.user, world.db, assignment.UnassignBody(confirm=True))
    assert caught.value.status_code == 409 and "cannot be unassigned" in caught.value.detail["message"]
    assert order.assignment_status == "assigned"


def test_open_order_can_be_unassigned_and_has_no_reason(world):
    (order,) = world.add(so("g-2"))
    assert unassign_guard.unassign_blocked_reason(order) is None
    assert assignment.unassign_order("g-2", world.user, world.db, assignment.UnassignBody(confirm=True))["success"] is True
    assert order.assignment_status == "unassigned"


def test_checked_out_stop_is_rejected(world):
    world.add(so("g-3"))
    world.stops["g-3"] = "checked_out"
    with pytest.raises(HTTPException) as caught:
        assignment.unassign_order("g-3", world.user, world.db, assignment.UnassignBody(confirm=True))
    assert caught.value.status_code == 409


def test_checked_out_stop_reason_from_in_memory_trip_facts(world, monkeypatch):
    (order,) = world.add(so("g-4"))
    from services import order_tracker_facts
    monkeypatch.setattr(order_tracker_facts, "current", lambda batch: {"stops": {"g-4": {"status": "checked_out"}}} if batch == "b1" else None)
    assert unassign_guard.unassign_blocked_reason(order) == unassign_guard.CHECKED_OUT_REASON
    row = load_planning._with_unassign_reason({}, order)
    assert row["unassign_blocked_reason"] == unassign_guard.CHECKED_OUT_REASON


def test_bulk_skips_blocked_orders_and_lists_them(world):
    world.add(so("g-5"), so("g-6", raw={"status": "fulfilled"}), so("g-7"), so("g-8", raw={"shipped_status": "partially_shipped"}))
    world.stops["g-7"] = "checked_out"
    result = assignment.bulk_unassign(assignment.BulkUnassignBody(order_ids=["g-5", "g-6", "g-7", "g-8"], confirm=True), world.user, world.db)
    assert result["unassigned"] == ["g-5"]
    skipped = {s["id"]: s for s in result["skipped"]}
    assert set(skipped) == {"g-6", "g-7", "g-8"}
    assert skipped["g-6"]["order_number"] == "SO26-g-6" and "cannot be unassigned" in skipped["g-6"]["reason"]
    assert "partially shipped" in skipped["g-8"]["reason"]


def test_automatic_flag_bypasses_the_manual_guard(world):
    (shipped,) = world.add(so("g-9", raw={"shipped_status": "shipped"}))
    (not_delivered,) = world.add(so("g-10"))
    world.stops["g-10"] = "checked_out"  # checked out as not delivered at trip end
    assert assignment.unassign_one("g-9", world.user, world.db, automatic=True)["success"] is True
    result = assignment.bulk_unassign_orders(["g-10"], world.user, world.db, automatic=True)
    assert result["unassigned"] == ["g-10"] and shipped.assignment_status == "unassigned" and not_delivered.assignment_status == "unassigned"


def test_automatic_still_refuses_zoho_delivered(world):
    world.add(so("g-11", raw={"status": "delivered"}))
    with pytest.raises(HTTPException):
        assignment.unassign_one("g-11", world.user, world.db, automatic=True)


# ---- pickup -------------------------------------------------------------------------------------------------------------


def test_pickup_single_assign_is_422_with_structured_detail(world):
    world.add(so("g-20", assigned=False, raw={"custom_fields": [{"label": "Fulfillment Type", "api_name": "cf_fulfillment_type", "value": "Pickup"}]}))
    with pytest.raises(HTTPException) as caught:
        world.assign(["g-20"])
    detail = caught.value.detail
    assert caught.value.status_code == 422 and detail["title"] == "Unable to assign" and detail["code"] == "pickup_not_assignable"
    blocked = detail["blocked"][0]
    assert blocked["order_number"] == "SO26-g-20" and blocked["customer"] == "Customer g-20" and blocked["delivery_date"] == DAY.isoformat()
    assert blocked["fulfillment_type"] == "Pickup" and "delivery_method" in blocked
    assert blocked["reason"] == "SO26-g-20 is a customer pickup (Fulfillment type: Pickup). Pickup orders are collected by the customer and can't be assigned to a truck."
    assert lc._assigned_zoho["g-20"].assignment_status == "unassigned"


@pytest.mark.parametrize("method", ["Pickup", "Customer pick up", "PICK-UP"])
def test_pickup_delivery_method_variants(method):
    assert unassign_guard.pickup_block(so("x", assigned=False, delivery_method=method)) is not None
    assert unassign_guard.pickup_block(so("x", assigned=False, delivery_method="Truck delivery")) is None


def test_bulk_assign_assigns_the_rest_and_returns_blocked(world):
    world.add(so("g-21", assigned=False), so("g-22", assigned=False, raw={"delivery_method": "Pick up"}), so("g-23", assigned=False, raw={"cf_fulfillment_type": "pickup"}))
    result = world.assign(["g-22", "g-21", "g-23"])  # the path id itself is a pickup
    assert result["salesorder_ids"] == ["g-21"]
    assert sorted(b["id"] for b in result["pickup_blocked"]) == ["g-22", "g-23"]
    assert lc._assigned_zoho["g-21"].assignment_status == "assigned" and lc._assigned_zoho["g-22"].assignment_status == "unassigned"


def test_po_for_pickup_is_not_a_customer_pickup():
    po = SimpleNamespace(id="PO:1", salesorder_number="PO-1", order_type="PO", raw_json={"delivery_method": "For Pickup"}, delivery_method=None)
    assert assignment._pickup_block(po) is None
