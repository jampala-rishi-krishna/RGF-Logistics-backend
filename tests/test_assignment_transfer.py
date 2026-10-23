from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routers import assignment
from services import day_model, live_sales_order_cache, trip_events


def _row(status="confirmed", shipment=None):
    return SimpleNamespace(id="so-1", salesorder_number="SO-1", assignment_status="assigned", vehicle_id="TRK1", driver_id=9,
                           helper_ids=[9, 17], expected_shipment_date=date(2026, 10, 24), raw_json={"status": status},
                           shipment_status=shipment, assigned_by=2, assigned_by_name="Old manager", assignment_batch_id="batch-1")


class _Result:
    def __init__(self, rows=(), scalar=None): self.rows, self.scalar = list(rows), scalar
    def scalars(self): return self
    def all(self): return self.rows
    def scalar_one_or_none(self): return self.scalar


class _Db:
    def __init__(self, row):
        self.row = row
        self.calls = 0
        self.events = []
    def execute(self, _statement):
        self.calls += 1
        if self.calls == 1: return _Result([self.row])
        if self.calls == 2: return _Result(scalar=SimpleNamespace(plate_no="TRK2"))
        if self.calls == 3: return _Result([])
        return _Result(scalar=None)
    def commit(self): pass


def _patch(monkeypatch, row, events):
    monkeypatch.setattr(assignment.staff_directory_cache, "get_by_id", lambda sid: {9: {"id": 9, "active": True, "role": "DRIVER"}, 17: {"id": 17, "active": True, "role": "HELPER"}, 18: {"id": 18, "active": True, "role": "HELPER"}}.get(sid))
    monkeypatch.setattr(assignment.live_sales_order_cache, "find_cached", lambda _id: row)
    monkeypatch.setattr(assignment.live_sales_order_cache, "set_assignment", lambda _id, **fields: None)
    monkeypatch.setattr(assignment.live_sales_order_cache, "bump_assignment_generation", lambda: None)
    monkeypatch.setattr(assignment, "invalidate_fleet_cache", lambda: None)
    monkeypatch.setattr(assignment, "_invalidate_assignment_options_cache", lambda: None)
    monkeypatch.setattr(trip_events, "bump", lambda: None)
    monkeypatch.setattr(trip_events, "add_event", lambda db, **kwargs: events.append(kwargs))
    monkeypatch.setattr(trip_events, "now_utc", lambda: __import__("datetime").datetime(2026, 10, 24, tzinfo=__import__("datetime").timezone.utc))


def test_transfer_changes_crew_in_place_and_writes_one_event(monkeypatch):
    row, events = _row(), []
    _patch(monkeypatch, row, events)
    result = assignment.transfer_assignment(assignment.TransferAssignmentBody(salesorder_ids=["so-1"], vehicle_id="TRK2", driver_id=9, helper_ids=[18], reason="phone issue"), SimpleNamespace(id=1, role="admin", is_manager=True, full_name="Manager", email="m@example.test", staff_id=None), _Db(row))
    assert result["success"] and row.vehicle_id == "TRK2" and row.helper_ids == [9, 18]
    assert len(events) == 1 and events[0]["event_type"] == "assignment_transferred"
    assert events[0]["payload"]["from"]["helper_ids"] == [9, 17] and events[0]["payload"]["to"]["helper_ids"] == [9, 18]
    assert day_model.is_crew(row, 18) and not day_model.is_crew(row, 17)


def test_non_manager_is_forbidden(monkeypatch):
    row = _row()
    with pytest.raises(HTTPException) as exc:
        assignment.transfer_assignment(assignment.TransferAssignmentBody(salesorder_ids=["so-1"], reason="phone issue"), SimpleNamespace(id=2, role="dispatcher", is_manager=False), _Db(row))
    assert exc.value.status_code == 403


def test_shipped_order_stays_with_original_crew(monkeypatch):
    row = _row(shipment="shipped")
    with pytest.raises(HTTPException) as exc:
        assignment.transfer_assignment(assignment.TransferAssignmentBody(salesorder_ids=["so-1"], reason="phone issue"), SimpleNamespace(id=1, role="admin", is_manager=True), _Db(row))
    assert exc.value.status_code == 409 and row.helper_ids == [9, 17]
