import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from auth import dependencies
from database import get_db
from routers import order_tracker


def client(env, role):
    app = FastAPI()
    env.user.role = role
    app.dependency_overrides[dependencies.get_current_user] = lambda: env.user
    app.dependency_overrides[dependencies.get_current_user_optional] = lambda: env.user
    app.dependency_overrides[get_db] = lambda: object()  # current date must not call it
    app.include_router(order_tracker.router, dependencies=[Depends(dependencies.workspace_access)])
    return TestClient(app)


@pytest.mark.parametrize("role,status", [("admin", 200), ("planner", 200), ("sales", 200), ("wet_market", 200), ("driver", 403), ("helper", 403), ("warehouse", 403), ("logistics_officer", 403)])
def test_workspace_roles(tracker_env, role, status):
    response = client(tracker_env, role).get("/api/order-tracker/orders")
    assert response.status_code == status


def test_order_ownership_and_validation_on_http_boundary(tracker_env):
    api = client(tracker_env, "sales")
    assert api.get("/api/order-tracker/orders/SO-200").status_code == 404
    assert api.get("/api/order-tracker/orders/SO-100").status_code == 200
    assert api.get("/api/order-tracker/orders?date_from=not-a-date").status_code == 422
    assert api.post("/api/order-tracker/ask", json={"question": "Where is SO-100?", "user_id": 1}).status_code == 422
    assert api.post("/api/order-tracker/ask", json={"question": "Mark SO-100 paid"}).status_code == 200
    assert api.post("/api/order-tracker/orders", json={}).status_code == 405


def test_no_auth_is_denied():
    app = FastAPI(); app.include_router(order_tracker.router)
    assert TestClient(app).get("/api/order-tracker/orders").status_code == 401
