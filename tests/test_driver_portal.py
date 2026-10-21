"""Driver portal: logins, invites, rate limiting, per-driver scoping, history, helpers, delivery details.

Everything runs against an in-memory SQLite database and a fake staff directory. The real (Neon) database is never
opened (conftest.py enforces that) and any Zoho call fails the test.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("JWT_SECRET", "test-secret-not-real")

import database  # noqa: E402
from auth import dependencies, security  # noqa: E402
from models.sales_order_history import SalesOrderHistory  # noqa: E402
from models.sales_order_lines import SalesOrderLine  # noqa: E402
from models.user import User, UserInvite  # noqa: E402
from routers import admin, agent, assignment, auth as auth_router, driver as driver_router, load_planning, pipeline, reports  # noqa: E402
from services import delivery_details, driver_assignments, gmail_sender, live_sales_order_cache, login_rate_limiter, sales_order_history_sync, staff_directory_cache, user_cache, user_invites  # noqa: E402
from services.phone_numbers import ph_phone_digits, ph_phone_lookup_values  # noqa: E402
from services.staff_roles import helper_ids_of, staff_kind  # noqa: E402

PASSWORD = "correct horse battery"  # test-only value


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


STAFF = {
    9: {"id": 9, "name": "Emert Hirondo", "title": "MC RIDER", "warehouse": "GLACIER", "email": "emert@example.test", "phone": "09482834802", "role": "DRIVER"},
    10: {"id": 10, "name": "Paul Aljecera", "title": "MC RIDER", "warehouse": "METS", "email": "paul@example.test", "phone": "09367496403", "role": "DRIVER"},
    17: {"id": 17, "name": "Raymond Roblo", "title": "DELIVERY HELPER", "warehouse": "METS", "email": "ray@example.test", "phone": "09170000017", "role": "HELPER"},
    18: {"id": 18, "name": "Jaymar Arca", "title": "DELIVERY HELPER", "warehouse": "METS", "email": "jay@example.test", "phone": "09170000018", "role": "HELPER"},
}


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from models.trip import DriverAlertRead, DriverIssue, FormTemplate, FormTemplateItem, InvoiceHandover, SoAcknowledgement, Trip, TripEvent, TripStop  # the driver payloads now read the trip workflow tables

    from models.access_workflow import UnassignRequest
    for table in (UnassignRequest.__table__, User.__table__, UserInvite.__table__, SalesOrderHistory.__table__, SalesOrderLine.__table__, FormTemplate.__table__, FormTemplateItem.__table__,
                  Trip.__table__, SoAcknowledgement.__table__, TripStop.__table__, TripEvent.__table__, InvoiceHandover.__table__, DriverIssue.__table__, DriverAlertRead.__table__):
        table.create(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    monkeypatch.setattr(dependencies, "SessionLocal", Session)
    monkeypatch.setattr(staff_directory_cache, "get_by_id", lambda staff_id, **_: STAFF.get(staff_id))
    monkeypatch.setattr(live_sales_order_cache, "find_cached", lambda order_id: None)
    monkeypatch.setenv("DISPATCH_PHONE", "+639170000000")
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.test")
    user_cache._cache.clear()
    login_rate_limiter.reset_all()
    driver_assignments.reset_cache()

    zoho_calls: list[str] = []

    def no_zoho(*args, **kwargs):
        zoho_calls.append("zoho")
        raise AssertionError("a driver endpoint tried to call Zoho")

    from services import zoho_client

    monkeypatch.setattr(zoho_client, "_request_http", no_zoho)
    monkeypatch.setattr(live_sales_order_cache, "fetch_sales_order_detail", no_zoho)
    monkeypatch.setattr(live_sales_order_cache, "ensure_zoho_data", no_zoho)

    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(driver_router.router)
    app.include_router(admin.router)
    app.include_router(admin.usage_router)
    for extra in (reports.router, agent.router, pipeline.router, load_planning.router, assignment.router):
        app.include_router(extra)

    def get_test_db():
        with Session() as db:
            yield db

    app.dependency_overrides[database.get_db] = get_test_db
    client = TestClient(app)
    yield SimpleNamespace(client=client, Session=Session, zoho_calls=zoho_calls, engine=engine)
    engine.dispose()


def make_user(env, *, name="Test Driver", email="driver@example.test", phone=639482834802, role="driver", staff_id=9, status="active", password=PASSWORD, must_change=False):
    with env.Session() as db:
        user = User(full_name=name, email=email, phone=phone, role=role, status=status, staff_id=staff_id, password_hash=security.hash_password(password) if password else security.unusable_password_hash(), must_change_password=must_change)
        db.add(user)
        db.commit()
        return user.id


def login(env, identifier, password=PASSWORD, ip="10.0.0.1"):
    return env.client.post("/auth/login", json={"identifier": identifier, "password": password}, headers={"x-forwarded-for": ip})


def bearer(env, user_id, role="driver", email="x@example.test"):
    return {"Authorization": f"Bearer {security.create_access_token(user_id=user_id, role=role, email=email)}"}


def add_order(env, so_id, *, driver_id, helper_ids=None, batch="batch-1", day=None, vehicle="NAA1234", lines=(("Frozen Chicken", 10, "pack", 12.5),), status="assigned", city="Makati", contact=None, notes=None, assigned_offset=0, email_status="sent", number=None):
    day = day or driver_assignments.manila_today()
    with env.Session() as db:
        db.add(SalesOrderHistory(
            id=so_id, salesorder_number=number or f"SO-{so_id}", customer_name=f"Customer {so_id}", expected_shipment_date=day, synced_at=datetime.now(timezone.utc),
            shipping_address={"address": f"{so_id} Main St", "city": city, "country": "Philippines"}, vehicle_id=vehicle, driver_id=driver_id,
            helper_ids=helper_ids if helper_ids is not None else [driver_id], assigned_at=datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc) + timedelta(minutes=assigned_offset),
            assignment_status=status, assignment_batch_id=batch, email_status=email_status, delivery_contact=contact, delivery_notes=notes,
        ))
        db.flush()
        for name, qty, unit, kg in lines:
            db.add(SalesOrderLine(sales_order_id=so_id, name=name, quantity=qty, unit=unit, weight_kg=kg))
        db.commit()


# ---- login ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("identifier", ["driver@example.test", "DRIVER@Example.test", "09482834802", "+639482834802", "639482834802", "0948 283 4802"])
def test_login_by_email_or_phone(env, identifier):
    make_user(env)
    response = login(env, identifier)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["role"] == "driver" and body["user"]["staffId"] == 9 and body["token"]


def test_login_wrong_password_and_unknown_account_look_identical(env):
    make_user(env)
    wrong = login(env, "driver@example.test", "nope-nope-nope")
    unknown = login(env, "nobody@example.test", "nope-nope-nope", ip="10.0.0.2")
    unknown_phone = login(env, "09171112222", "nope-nope-nope", ip="10.0.0.3")
    assert wrong.status_code == unknown.status_code == unknown_phone.status_code == 401
    assert wrong.json() == unknown.json() == unknown_phone.json() == {"detail": "Invalid credentials"}


def test_seeded_driver_with_unusable_password_cannot_log_in(env):
    make_user(env, password=None)
    assert login(env, "driver@example.test", "").status_code == 400
    assert login(env, "driver@example.test", "anything-at-all").status_code == 401


def test_disabled_user_is_refused_at_login(env):
    make_user(env, status="disabled")
    response = login(env, "driver@example.test")
    assert response.status_code == 403 and "disabled" in response.json()["detail"].lower()
    assert login(env, "driver@example.test", "wrong-password").status_code == 401  # wrong password still reveals nothing


def test_driver_token_lasts_30_days_and_other_roles_stay_short(env):
    driver_token = security.decode_access_token(security.create_access_token(user_id=1, role="driver", email="a@b.test"))
    admin_token = security.decode_access_token(security.create_access_token(user_id=1, role="admin", email="a@b.test"))
    now = datetime.now(timezone.utc).timestamp()
    assert 29.9 * 86400 < driver_token["exp"] - now < 30.1 * 86400
    assert admin_token["exp"] - now < 2 * 86400


# ---- rate limiting -------------------------------------------------------------------------------------------


def test_rate_limit_per_account_blocks_even_the_right_password(env):
    make_user(env)
    for _ in range(5):
        assert login(env, "driver@example.test", "wrong-password").status_code == 401
    blocked = login(env, "driver@example.test")  # correct password, but the account is locked
    assert blocked.status_code == 429 and blocked.json()["detail"] == "Too many attempts. Please try again later."
    # the same lock applies to the phone form of the same account and to an account that does not exist
    assert login(env, "09482834802", ip="10.0.0.9").status_code == 429
    for _ in range(5):
        login(env, "ghost@example.test", "wrong-password", ip="10.0.0.8")
    assert login(env, "ghost@example.test", "wrong-password", ip="10.0.0.8").status_code == 429


def test_rate_limit_per_ip_across_accounts_and_success_clears_account(env):
    make_user(env)
    for i in range(5):
        assert login(env, f"user{i}@example.test", "wrong-password", ip="10.9.9.9").status_code == 401
    assert login(env, "driver@example.test", ip="10.9.9.9").status_code == 429  # that IP is locked
    assert login(env, "driver@example.test", ip="10.1.1.1").status_code == 200  # others are not
    for _ in range(3):
        login(env, "driver@example.test", "wrong-password", ip="10.2.2.2")
    assert login(env, "driver@example.test", ip="10.2.2.2").status_code == 200
    login_rate_limiter.record_failure("acct:639482834802")
    assert not login_rate_limiter.is_blocked("acct:639482834802")  # success cleared the three earlier failures


# ---- invite / reset links ------------------------------------------------------------------------------------


def create_token(env, user_id, purpose="invite"):
    with env.Session() as db:
        token, _ = user_invites.create_invite(db, db.get(User, user_id), purpose)
        db.commit()
        return token


def test_invite_token_is_single_use_and_only_its_hash_is_stored(env):
    uid = make_user(env, password=None)
    token = create_token(env, uid)
    with env.Session() as db:
        stored = db.query(UserInvite).one()
        assert token not in stored.token_hash and stored.token_hash == user_invites.hash_token(token)
    first = env.client.post("/auth/set-password", json={"token": token, "password": "brand-new-pass"})
    assert first.status_code == 200
    second = env.client.post("/auth/set-password", json={"token": token, "password": "another-pass-1"})
    assert second.status_code == 400 and "invalid or has expired" in second.json()["detail"]
    assert login(env, "driver@example.test", "brand-new-pass").status_code == 200
    assert login(env, "driver@example.test", "another-pass-1", ip="10.0.0.5").status_code == 401  # the second attempt changed nothing


def test_invite_token_expires_after_24_hours(env):
    uid = make_user(env, password=None)
    now = datetime.now(timezone.utc)
    with env.Session() as db:
        token, expires_at = user_invites.create_invite(db, db.get(User, uid), "invite", now=now - timedelta(hours=24, minutes=1))
        db.commit()
    assert env.client.post("/auth/set-password", json={"token": token, "password": "brand-new-pass"}).status_code == 400
    with env.Session() as db:
        assert user_invites.redeem(db, token, now=now - timedelta(hours=23)) is not None  # still valid just inside the window


def test_new_link_revokes_the_previous_one_and_bad_inputs_do_not_consume(env):
    uid = make_user(env, password=None)
    old = create_token(env, uid)
    new = create_token(env, uid, "reset")
    assert env.client.post("/auth/set-password", json={"token": old, "password": "brand-new-pass"}).status_code == 400
    assert env.client.post("/auth/set-password", json={"token": new, "password": "short"}).status_code == 400  # too short: not consumed
    assert env.client.post("/auth/set-password", json={"token": new, "password": "brand-new-pass"}).status_code == 200
    assert env.client.post("/auth/set-password", json={"token": "x" * 40, "password": "brand-new-pass"}).status_code == 400


def test_set_password_refused_for_disabled_user(env):
    uid = make_user(env, password=None, status="disabled")
    assert env.client.post("/auth/set-password", json={"token": create_token(env, uid), "password": "brand-new-pass"}).status_code == 400


def test_admin_can_create_invite_link_and_disable_but_not_a_driver(env):
    driver_id = make_user(env)
    admin_id = make_user(env, name="Admin", email="admin@example.test", phone=639000000001, role="admin", staff_id=None)
    ok = env.client.post(f"/api/admin/users/{driver_id}/invite-link", json={"purpose": "invite"}, headers=bearer(env, admin_id, "admin"))
    assert ok.status_code == 200
    link = ok.json()["link"]
    assert link.startswith("https://app.example.test/set-password?token=")
    token = link.split("token=")[1]
    assert env.client.post("/auth/set-password", json={"token": token, "password": "brand-new-pass"}).status_code == 200
    assert env.client.post(f"/api/admin/users/{driver_id}/invite-link", json={"purpose": "invite"}, headers=bearer(env, driver_id)).status_code == 403
    assert env.client.get("/users", headers=bearer(env, driver_id)).status_code == 403
    users = env.client.get("/users", headers=bearer(env, admin_id, "admin")).json()
    row = next(u for u in users if u["id"] == driver_id)
    assert row["staff_id"] == 9 and row["invite_state"] == "used" and "password_hash" not in row


def test_admin_can_create_driver_with_initial_password_and_email_details(env, monkeypatch):
    admin_id = make_user(env, name="Admin", email="admin@example.test", phone=639000000001, role="admin", staff_id=None)
    sent: list[dict] = []
    monkeypatch.setattr(gmail_sender, "configured", lambda: True)
    monkeypatch.setattr(gmail_sender, "send_email", lambda **kwargs: sent.append(kwargs) or {"id": "msg-1"})

    response = env.client.post(
        "/api/admin/users",
        json={
            "full_name": "Emert Hirondo",
            "email": "emert@example.test",
            "phone": "09482834802",
            "role": "driver",
            "staff_id": 9,
            "password": "driver-pass-123",
            "send_login_email": True,
        },
        headers=bearer(env, admin_id, "admin"),
    )
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["staff_id"] == 9 and row["role"] == "driver" and "password_hash" not in row
    assert login(env, "emert@example.test", "driver-pass-123").status_code == 200
    assert login(env, "09482834802", "driver-pass-123").status_code == 200
    assert len(sent) == 1
    assert sent[0]["to"] == "emert@example.test"
    assert "Your RareChain login details" in sent[0]["subject"]
    assert "driver-pass-123" in sent[0]["html"] and "09482834802" in sent[0]["html"]
    assert "MC RIDER" in sent[0]["html"] and "GLACIER" in sent[0]["html"]


def test_disabling_a_driver_cuts_off_their_existing_token_immediately(env):
    driver_id = make_user(env)
    admin_id = make_user(env, name="Admin", email="admin@example.test", phone=639000000001, role="admin", staff_id=None)
    headers = bearer(env, driver_id)
    assert env.client.get("/api/driver/me", headers=headers).status_code == 200  # fills the 10-minute user cache
    assert env.client.post(f"/api/admin/users/{driver_id}/status", json={"status": "disabled"}, headers=bearer(env, admin_id, "admin")).status_code == 200
    assert env.client.get("/api/driver/me", headers=headers).status_code == 401
    assert login(env, "driver@example.test").status_code == 403
    assert env.client.post(f"/api/admin/users/{admin_id}/status", json={"status": "disabled"}, headers=bearer(env, admin_id, "admin")).status_code == 409


def test_a_driver_token_is_refused_on_operational_routers(env):
    driver_id = make_user(env)
    h = bearer(env, driver_id)
    calls = [
        ("get", "/api/reports/rgf-logistics"), ("get", "/api/reports/rgf-logistics/sales-order/x"), ("post", "/agent/chat"), ("post", "/agent/confirm-action"),
        ("get", "/api/load-planning/dispatch-dashboard"), ("get", "/api/load-planning/planning/orders"), ("get", "/api/load-planning/assignments/anything"),
        ("post", "/api/load-planning/assignments/anything"), ("get", "/users"), ("post", "/api/admin/test-assignment-email"),
    ]
    for method, path in calls:
        response = getattr(env.client, method)(path, headers=h, **({"json": {}} if method == "post" else {}))
        assert response.status_code == 403, (method, path, response.status_code)
    assert env.zoho_calls == []


def test_must_change_password_flag_is_returned_for_the_frontend_redirect(env):
    make_user(env, must_change=True)
    assert login(env, "driver@example.test").json()["user"]["mustChangePassword"] is True


# ---- driver scoping --------------------------------------------------------------------------------------------


def two_drivers(env):
    a = make_user(env, name="Emert", email="a@example.test", phone=639482834802, staff_id=9)
    b = make_user(env, name="Paul", email="b@example.test", phone=639367496403, staff_id=10)
    add_order(env, "so-a1", driver_id=9, batch="batch-A")
    add_order(env, "so-b1", driver_id=10, batch="batch-B", vehicle="NBB5678")
    return a, b


def test_driver_only_sees_their_own_assignments_whatever_the_url(env):
    a, b = two_drivers(env)
    h = bearer(env, a)
    today = env.client.get("/api/driver/today", headers=h).json()
    assert [batch["batch_id"] for batch in today["batches"]] == ["batch-A"]
    assert env.client.get("/api/driver/today?driver_id=10", headers=h).status_code == 403
    assert env.client.get("/api/driver/today?driver_id=9", headers=h).status_code == 200  # naming yourself is fine
    assert env.client.get("/api/driver/history?driver_id=10&days=7", headers=h).status_code == 403
    assert env.client.get("/api/driver/me?driver_id=10", headers=h).status_code == 403
    assert env.client.get("/api/driver/assignments/batch-B", headers=h).status_code == 404  # someone else's batch
    assert env.client.get("/api/driver/assignments/batch-B?driver_id=10", headers=h).status_code == 403
    assert env.client.get("/api/driver/assignments/batch-A", headers=h).status_code == 200
    mine = env.client.get("/api/driver/assignments/batch-A", headers=h).json()
    assert [order["id"] for order in mine["orders"]] == ["so-a1"]
    assert env.zoho_calls == []


def test_admin_can_view_any_driver_but_must_name_one(env):
    a, b = two_drivers(env)
    admin_id = make_user(env, name="Admin", email="admin@example.test", phone=639000000001, role="admin", staff_id=None)
    h = bearer(env, admin_id, "admin")
    assert env.client.get("/api/driver/today", headers=h).status_code == 400
    assert [x["batch_id"] for x in env.client.get("/api/driver/today?driver_id=10", headers=h).json()["batches"]] == ["batch-B"]
    assert env.client.get("/api/driver/assignments/batch-A?driver_id=9", headers=h).status_code == 200
    assert env.client.get("/api/driver/assignments/batch-A?driver_id=10", headers=h).status_code == 404  # still scoped to the named driver
    assert env.client.get("/api/driver/me?driver_id=9", headers=h).json()["name"] == "Emert Hirondo"


def test_other_roles_and_unlinked_drivers_are_refused(env):
    dispatcher = make_user(env, name="Disp", email="disp@example.test", phone=639000000002, role="dispatcher", staff_id=None)
    unlinked = make_user(env, name="Unlinked", email="u@example.test", phone=639000000003, staff_id=None)
    assert env.client.get("/api/driver/today", headers=bearer(env, dispatcher, "dispatcher")).status_code == 403
    assert env.client.get("/api/driver/today", headers=bearer(env, unlinked)).status_code == 403
    assert env.client.get("/api/driver/today").status_code == 401


# ---- today / detail / history content --------------------------------------------------------------------------


def test_today_groups_by_batch_with_totals_helpers_and_empty_state(env):
    a, _ = two_drivers(env)
    h = bearer(env, a)
    empty = env.client.get("/api/driver/today", headers=bearer(env, make_user(env, name="Nobody", email="n@example.test", phone=639000000004, staff_id=17))).json()
    assert empty["batches"] == []
    add_order(env, "so-a2", driver_id=9, batch="batch-A", city="Pasig", assigned_offset=5, lines=(("Beef Strips", 4, "kg", 4.0), ("Wings", 6, "pack", 3.0)), helper_ids=[9, 17, 18])
    add_order(env, "so-a3", driver_id=9, batch="batch-C", vehicle="NCC9999", assigned_offset=9)
    batches = env.client.get("/api/driver/today", headers=h).json()["batches"]
    by_id = {b["batch_id"]: b for b in batches}
    assert set(by_id) == {"batch-A", "batch-C"}
    batch_a = by_id["batch-A"]
    assert batch_a["vehicle"] == "NAA1234" and batch_a["so_count"] == 2
    assert batch_a["total_packs"] == 16.0 and batch_a["total_kg"] == 12.5 + 7.0  # packs only count pack units; kg is the sum of line weights
    assert (batch_a["first_area"], batch_a["last_area"]) == ("Makati", "Pasig")
    assert batch_a["departure_time"] is None and batch_a["stop_order_set"] is False
    assert batch_a["email_status"] == "sent"


def test_helpers_come_from_a_set_difference_not_from_position(env):
    a, _ = two_drivers(env)
    add_order(env, "so-h1", driver_id=9, batch="batch-H", helper_ids=[17, 9, 18])  # driver in the middle
    add_order(env, "so-h2", driver_id=9, batch="batch-H2", helper_ids=[18, 17, 9])  # same people, different order
    batches = {b["batch_id"]: b for b in env.client.get("/api/driver/today", headers=bearer(env, a)).json()["batches"]}
    for key in ("batch-H", "batch-H2"):
        assert [(x["id"], x["name"], x["phone"]) for x in batches[key]["helpers"]] == [(17, "Raymond Roblo", "09170000017"), (18, "Jaymar Arca", "09170000018")]
    assert batches["batch-A"]["helpers"] == []  # helper_ids == [driver] only
    assert helper_ids_of(9, [17, 9, 18]) == helper_ids_of(9, [18, 17, 9]) == [17, 18]
    assert helper_ids_of(None, [3, 2]) == [2, 3] and helper_ids_of(9, None) == []


def test_assignment_detail_has_contact_notes_lines_and_marks_uncached_details(env):
    a, _ = two_drivers(env)
    add_order(env, "so-d1", driver_id=9, batch="batch-D", contact={"person": "Ana Cruz", "phone": "09175550000", "attention": "Receiving"}, notes="Gate B, call on arrival", lines=(("Pork Belly", 20, "kg", 20.0), ("Chicken", 5, "pack", 6.0)))
    add_order(env, "so-d2", driver_id=9, batch="batch-D", assigned_offset=3, contact=None)
    detail = env.client.get("/api/driver/assignments/batch-D", headers=bearer(env, a)).json()
    first, second = detail["orders"]
    assert first["so_number"] == "SO-so-d1" and first["contact"] == {"person": "Ana Cruz", "phone": "09175550000", "attention": "Receiving"}
    assert first["notes"] == "Gate B, call on arrival" and first["address_text"].startswith("so-d1 Main St")
    assert first["lines"][1] == {"name": "Chicken", "quantity": 5.0, "unit": "pack", "packs": 5.0, "kg": 6.0, "warehouse": None}
    assert first["total_kg"] == 26.0 and first["total_packs"] == 5.0 and first["details_loading"] is False
    assert second["contact"] is None and second["details_loading"] is True and detail["details_loading"] is True
    assert env.zoho_calls == []


def test_history_is_past_only_newest_first_and_filterable(env):
    a, _ = two_drivers(env)
    today = driver_assignments.manila_today()
    add_order(env, "so-p1", driver_id=9, batch="batch-P1", day=today - timedelta(days=1))
    add_order(env, "so-p2", driver_id=9, batch="batch-P2", day=today - timedelta(days=10))
    add_order(env, "so-p3", driver_id=9, batch="batch-P3", day=today - timedelta(days=40))
    add_order(env, "so-x1", driver_id=10, batch="batch-PX", day=today - timedelta(days=2))  # someone else's
    add_order(env, "so-u1", driver_id=9, batch="batch-U", day=today - timedelta(days=2), status="unassigned")
    h = bearer(env, a)
    seven = env.client.get("/api/driver/history?days=7", headers=h).json()
    assert [b["batch_id"] for b in seven["batches"]] == ["batch-P1"]
    thirty = env.client.get("/api/driver/history?days=30", headers=h).json()
    assert [b["batch_id"] for b in thirty["batches"]] == ["batch-P1", "batch-P2"]  # newest first
    custom = env.client.get(f"/api/driver/history?from={today - timedelta(days=60)}&to={today}", headers=h).json()
    assert [b["batch_id"] for b in custom["batches"]] == ["batch-P1", "batch-P2", "batch-P3"]  # today itself is excluded, others' and unassigned too
    assert env.client.get("/api/driver/history?from=2026-01-05&to=2026-01-01", headers=h).status_code == 400
    assert env.client.get("/api/driver/history?from=2025-01-01&to=2026-12-31", headers=h).status_code == 400  # over 366 days
    assert env.client.get("/api/driver/history?from=nope&to=2026-01-01", headers=h).status_code == 400
    assert env.client.get("/api/driver/history/../today", headers=h).status_code in (200, 404)
    detail = env.client.get("/api/driver/assignments/batch-P2", headers=h)
    assert detail.status_code == 200 and detail.json()["delivery_date"] == (today - timedelta(days=10)).isoformat()
    assert env.zoho_calls == []


def test_legacy_assignments_without_a_batch_id_are_grouped_and_reachable(env):
    a, _ = two_drivers(env)
    add_order(env, "so-l1", driver_id=9, batch=None, vehicle="NLL1111")
    add_order(env, "so-l2", driver_id=9, batch=None, vehicle="NLL1111", assigned_offset=0)
    batches = [b for b in env.client.get("/api/driver/today", headers=bearer(env, a)).json()["batches"] if b["vehicle"] == "NLL1111"]
    assert len(batches) == 1 and batches[0]["so_count"] == 2 and batches[0]["batch_id"].startswith("legacy-")
    assert env.client.get(f"/api/driver/assignments/{batches[0]['batch_id']}", headers=bearer(env, a)).json()["so_count"] == 2


def test_me_gives_real_name_and_dispatch_phone_only_when_set(env, monkeypatch):
    a, _ = two_drivers(env)
    me = env.client.get("/api/driver/me", headers=bearer(env, a)).json()
    assert me["name"] == "Emert Hirondo" and me["dispatch_phone"] == "+639170000000" and me["today"] == driver_assignments.manila_today().isoformat()
    monkeypatch.delenv("DISPATCH_PHONE")
    assert env.client.get("/api/driver/me", headers=bearer(env, a)).json()["dispatch_phone"] is None


def test_today_is_served_from_memory_until_the_assignment_state_changes(env):
    a, _ = two_drivers(env)
    h = bearer(env, a)
    reads = []
    event.listen(env.engine, "before_cursor_execute", lambda conn, cur, stmt, *args: reads.append(stmt) if stmt.lstrip().upper().startswith("SELECT") and "sales_orders" in stmt else None)
    env.client.get("/api/driver/today", headers=h)
    first = len(reads)
    for _ in range(5):
        env.client.get("/api/driver/today", headers=h)
    assert len(reads) == first  # five more polls, zero more reads of sales_orders
    live_sales_order_cache.bump_assignment_generation()
    env.client.get("/api/driver/today", headers=h)
    assert len(reads) > first


# ---- delivery notes / contact written inside the assignment write ----------------------------------------------


def live_order(**overrides):
    base = dict(id="so-live", salesorder_number="SO-LIVE", reference_number=None, customer_name="Live Customer", order_status="confirmed", invoice_status=None, payment_status=None, shipment_status=None,
                order_date=date(2026, 10, 12), expected_shipment_date=date(2026, 10, 12), total=100, delivery_method=None, salesperson_name=None, customer_po_number=None, billing_address={}, shipping_address={"address": "1 Rizal Ave", "city": "Manila", "attention": "Mang Tony", "phone": "09171230000"},
                payment_terms_label=None, mode_of_transport=None, synced_at=datetime.now(timezone.utc), vehicle_id="NAA1234", driver_id=9, route_id=None, manifest_id=None, assigned_at=datetime.now(timezone.utc),
                assigned_by=1, assignment_status="assigned", completed_at=None, helper_ids=[9, 17],
                raw_json={"notes": "Leave at dock 3", "line_items": [{"name": "Chicken", "quantity": 3, "unit": "pack"}], "shipping_address": {"attention": "Mang Tony", "phone": "09171230000"}})
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def counted_sync(env, monkeypatch):
    monkeypatch.setattr(sales_order_history_sync, "stock_for_orders", lambda orders: {})
    monkeypatch.setattr(sales_order_history_sync, "calculate_line_weight_kg", lambda *a, **k: 1.5)
    monkeypatch.setattr(live_sales_order_cache, "cached_detail", lambda order_id: None)
    statements: list[str] = []
    event.listen(env.engine, "before_cursor_execute", lambda conn, cur, stmt, *args: statements.append(stmt.strip().split("(")[0].split(" SET")[0].upper()[:40]) if not stmt.lstrip().upper().startswith("SELECT") else None)
    return env, statements


def test_delivery_details_are_written_in_the_same_single_statement_as_the_assignment(counted_sync):
    env, statements = counted_sync
    with env.Session() as db:
        sales_order_history_sync.sync_history_row(db, live_order())
        db.commit()
    sales_order_inserts = [s for s in statements if s.startswith("INSERT INTO SALES_ORDERS") and "LINES" not in s]
    assert len(sales_order_inserts) == 1 and not [s for s in statements if s.startswith("UPDATE")]  # one INSERT, no follow-up UPDATE
    with env.Session() as db:
        row = db.get(SalesOrderHistory, "so-live")
        assert row.delivery_notes == "Leave at dock 3"
        assert row.delivery_contact == {"person": "Mang Tony", "phone": "09171230000", "attention": "Mang Tony"}
    statements.clear()
    with env.Session() as db:  # re-assigning an existing row: still exactly one UPDATE of sales_orders carrying the new columns
        sales_order_history_sync.sync_history_row(db, live_order(vehicle_id="NBB5678", raw_json={"notes": "New note", "line_items": [{"name": "Chicken", "quantity": 3, "unit": "pack"}]}))
        db.commit()
    assert len([s for s in statements if s.startswith("UPDATE SALES_ORDERS")]) == 1


def test_details_not_cached_at_assign_time_stay_null_and_are_never_fetched_or_blanked(counted_sync):
    env, _ = counted_sync
    with env.Session() as db:
        sales_order_history_sync.sync_history_row(db, live_order(raw_json={"customer_name": "list payload only"}))  # no line_items = detail not cached
        db.commit()
        row = db.get(SalesOrderHistory, "so-live")
        assert row.delivery_notes is None and row.delivery_contact is None
    with env.Session() as db:
        sales_order_history_sync.sync_history_row(db, live_order())
        db.commit()
    with env.Session() as db:  # a later sync without the detail keeps what was stored
        sales_order_history_sync.sync_history_row(db, live_order(raw_json={}))
        db.commit()
        row = db.get(SalesOrderHistory, "so-live")
        assert row.delivery_notes == "Leave at dock 3" and row.delivery_contact["phone"] == "09171230000"
    assert env.zoho_calls == []


def test_delivery_details_extraction_prefers_shipping_contact_then_contact_person():
    notes, contact = delivery_details.extract({"line_items": [{"name": "x"}], "notes": "  hi  ", "contact_person_details": [{"first_name": "Ana", "last_name": "Cruz", "mobile": "0917"}], "shipping_address": {}})
    assert notes == "hi" and contact == {"person": "Ana Cruz", "phone": "0917", "attention": None}
    assert delivery_details.extract({"notes": "x"}) == (None, None) and delivery_details.extract(None) == (None, None)


# ---- helper plumbing in the assignment router -------------------------------------------------------------------


def test_assignment_body_driver_first_then_helpers_and_back_compat():
    body = assignment.AssignmentBody(vehicle_id="V", driver_id=9, helper_ids=[17, 18, 9])
    assert body.primary_driver_id() == 9 and body.all_driver_ids() == [9, 17, 18]
    legacy = assignment.AssignmentBody(vehicle_id="V", driver_ids=[10, 17])
    assert legacy.primary_driver_id() == 10 and legacy.all_driver_ids() == [10, 17]
    assert assignment.AssignmentBody(vehicle_id="V").all_driver_ids() == []


def test_staff_kind_uses_role_or_title():
    assert staff_kind(STAFF[9]) == "driver" and staff_kind(STAFF[17]) == "helper"
    assert staff_kind({"title": "DELIVERY HELPER", "role": "DRIVER"}) == "helper"  # a helper created through "+ New Driver" keeps role DRIVER


def test_assignment_email_names_the_helper_with_phone():
    helpers = [SimpleNamespace(name="Raymond Roblo", phone="09170000017")]
    profile = SimpleNamespace(plate_no="NAA1234", capacity_note=None)
    order = SimpleNamespace(id="so-1", salesorder_number="SO-1", customer_name="C", shipping_address=None, raw_json={"shipping_address": {"address": "1 St"}, "line_items": [{"quantity": 2}]})
    text = assignment._assignment_email_text(profile, [order], weight_fn=lambda o: 1.0, helpers=helpers)
    assert "Helper: Raymond Roblo · 09170000017" in text
    _, html, body_html = assignment._assignment_email_html([SimpleNamespace(name="Emert Hirondo", warehouse="METS")], profile, [order], "me", "now", weight_fn=lambda o: 1.0, helpers=helpers)
    assert "Helper: Raymond Roblo" in body_html and "Raymond Roblo" in html
    assert "Helper" not in assignment._assignment_email_text(profile, [order], weight_fn=lambda o: 1.0)


# ---- phone normalisation ----------------------------------------------------------------------------------------


def test_ph_phone_normalisation():
    for raw in ("09482834802", "+639482834802", "639482834802", "9482834802", "0948-283-4802"):
        assert ph_phone_digits(raw) == 639482834802
        assert 639482834802 in ph_phone_lookup_values(raw)
    for bad in ("12345", "", None, "0848283480", "abc"):
        assert ph_phone_digits(bad) is None and ph_phone_lookup_values(bad) == []


def test_driver_pages_contain_no_mock_data():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "artifacts", "intellifleet", "src")
    if not os.path.isdir(root):
        pytest.skip("frontend sources not present")
    hits = []
    for folder, _, files in os.walk(root):
        for name in files:
            if name.endswith((".tsx", ".ts")) and ".before-" not in name:
                text = open(os.path.join(folder, name), encoding="utf-8", errors="ignore").read()
                hits += [(name, needle) for needle in ("Ramon", "Cavite", "IF-204", "DCD 8953") if needle in text]
    assert hits == []
