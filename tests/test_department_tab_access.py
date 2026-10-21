"""Sales / Wet Market logins: Control Tower, Fleet, Orders, Order Tracker - read only."""
from __future__ import annotations

import pytest

from auth.dependencies import department_can_read


@pytest.mark.parametrize("path", ["/vehicles", "/vehicles/12", "/vehicles/12/trail", "/alerts", "/routes", "/routes/warehouses", "/routes/7/stops",
                                  "/orders", "/api/orders/1", "/api/load-planning/inventory/sales-orders"])
def test_tower_fleet_and_orders_are_readable(path):
    assert department_can_read("GET", path)


@pytest.mark.parametrize("method,path", [("POST", "/vehicles"), ("POST", "/poll-now"), ("POST", "/alerts/3/acknowledge"), ("POST", "/routes/7/stops"),
                                         ("GET", "/api/driver-overview/day"), ("GET", "/api/fleet-health/issues"), ("GET", "/drivers"),
                                         ("GET", "/routes/search"), ("GET", "/manifests"), ("GET", "/api/load-planning/assignment"), ("DELETE", "/orders/1")])
def test_everything_else_stays_blocked(method, path):
    assert not department_can_read(method, path)
