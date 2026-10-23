from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from auth import dependencies
from auth.dependencies import CurrentUser
from routers import warehouse


@pytest.fixture
def warehouse_client():
    app = FastAPI()
    app.include_router(warehouse.router, dependencies=[Depends(dependencies.workspace_access)])
    unauthenticated_app = FastAPI()
    unauthenticated_app.include_router(warehouse.router, dependencies=[Depends(dependencies.workspace_access)])
    users = {
        role: CurrentUser(index, f"{role}@example.test", role, role.title(), "active")
        for index, role in enumerate(("admin", "warehouse", "dispatcher", "sales", "driver"), 1)
    }
    current = {"user": None}

    app.dependency_overrides[dependencies.get_current_user] = lambda: current["user"]
    app.dependency_overrides[dependencies.get_current_user_optional] = lambda: current["user"]

    def as_role(role: str):
        current["user"] = users.get(role)
        return TestClient(app)

    old_warehouses, old_events = warehouse._WAREHOUSES, warehouse._EVENTS
    warehouse._WAREHOUSES = []
    warehouse._EVENTS = []
    yield SimpleNamespace(as_role=as_role, unauthenticated=TestClient(unauthenticated_app), users=users)
    warehouse._WAREHOUSES = old_warehouses
    warehouse._EVENTS = old_events


def test_warehouse_reads_require_authentication(warehouse_client):
    client = warehouse_client.unauthenticated
    assert client.get("/warehouses").status_code == 401
    assert client.get("/warehouse-events").status_code == 401

    # The dependency must reject malformed credentials before the handler runs.
    assert client.get("/warehouses", headers={"Authorization": "Bearer invalid"}).status_code == 401


@pytest.mark.parametrize("role", ["admin", "warehouse"])
def test_admin_and_warehouse_can_read_empty_and_populated_data(warehouse_client, role):
    client = warehouse_client.as_role(role)
    assert client.get("/warehouses").json() == []
    assert client.get("/warehouse-events").json() == []

    warehouse._WAREHOUSES[:] = [{"id": 7, "name": "METS", "location": "Cold room", "zone": "A", "secret": "hidden"}]
    warehouse._EVENTS[:] = [{
        "id": 9,
        "warehouse_id": "METS",
        "event_type": "loading",
        "manifest_id": "m-1",
        "status": "pending",
        "event_time": "2026-10-23T00:00:00Z",
        "secret": "hidden",
    }]
    warehouses = client.get("/warehouses")
    events = client.get("/warehouse-events", params={"warehouseId": "METS"})
    assert warehouses.status_code == events.status_code == 200
    assert warehouses.json() == [{"id": 7, "name": "METS", "location": "Cold room", "zone": "A"}]
    assert events.json() == [{
        "id": 9,
        "warehouse_id": "METS",
        "event_type": "loading",
        "manifest_id": "m-1",
        "status": "pending",
        "event_time": "2026-10-23T00:00:00Z",
    }]


@pytest.mark.parametrize("role", ["dispatcher", "sales", "driver"])
def test_nonwarehouse_roles_cannot_read(warehouse_client, role):
    client = warehouse_client.as_role(role)
    assert client.get("/warehouses").status_code == 403
    assert client.get("/warehouse-events").status_code == 403


def test_write_policy_remains_warehouse_or_admin(warehouse_client):
    warehouse_client.as_role("warehouse").post("/warehouse-events", json={"warehouseId": "METS", "eventType": "loading"})
    assert warehouse_client.as_role("sales").post("/warehouse-events", json={"warehouseId": "METS", "eventType": "loading"}).status_code == 403

