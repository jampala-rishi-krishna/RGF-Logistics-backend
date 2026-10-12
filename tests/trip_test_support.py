"""Shared fixtures for the officer / overview / media / guard tests: in-memory SQLite, fake staff directory, a guard that
fails the test on any Zoho call, and direct seeding of trip data (no dependency on the crew endpoints)."""
from __future__ import annotations

import os
import zlib
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test-secret-not-real")

import database  # noqa: E402
from auth import dependencies, security  # noqa: E402
from models.sales_order_history import SalesOrderHistory  # noqa: E402
from models.sales_order_lines import SalesOrderLine  # noqa: E402
from models.trip import FormTemplate, FormTemplateItem, InvoiceHandover, InvoiceHandoverItem, SoAcknowledgement, Trip, TripEvent, TripPhoto, TripStop  # noqa: E402
from models.user import User, UserInvite  # noqa: E402
from services import driver_assignments, live_sales_order_cache, overview, staff_directory_cache, trip_events, user_cache  # noqa: E402
from tests import sqlite_helpers  # noqa: E402

TABLES = (User.__table__, UserInvite.__table__, SalesOrderHistory.__table__, SalesOrderLine.__table__, FormTemplate.__table__, FormTemplateItem.__table__, Trip.__table__,
          SoAcknowledgement.__table__, TripStop.__table__, TripEvent.__table__, TripPhoto.__table__, InvoiceHandover.__table__, InvoiceHandoverItem.__table__)

STAFF = {
    9: {"id": 9, "name": "Emert Hirondo", "title": "MC RIDER", "warehouse": "GLACIER", "email": "emert@example.test", "phone": "09482834802", "role": "DRIVER", "active": True},
    10: {"id": 10, "name": "Paul Aljecera", "title": "MC RIDER", "warehouse": "METS", "email": "paul@example.test", "phone": "09367496403", "role": "DRIVER", "active": True},
    11: {"id": 11, "name": "Idle Driver", "title": "MC RIDER", "warehouse": "METS", "email": "idle@example.test", "phone": "09170000011", "role": "DRIVER", "active": True},
    12: {"id": 12, "name": "Glacier Idle", "title": "MC RIDER", "warehouse": "GLACIER", "email": "gi@example.test", "phone": "09170000012", "role": "DRIVER", "active": True},
    13: {"id": 13, "name": "Retired Driver", "title": "MC RIDER", "warehouse": "METS", "email": "ret@example.test", "phone": "09170000013", "role": "DRIVER", "active": False},
    17: {"id": 17, "name": "Raymond Roblo", "title": "DELIVERY HELPER", "warehouse": "METS", "email": "ray@example.test", "phone": "09170000017", "role": "HELPER", "active": True},
    18: {"id": 18, "name": "Jaymar Arca", "title": "DELIVERY HELPER", "warehouse": "METS", "email": "jay@example.test", "phone": "09170000018", "role": "HELPER", "active": True},
}
PASSWORD = "correct horse battery"


def today() -> date:
    return driver_assignments.manila_today()


def utc(day: date, hour: int, minute: int = 0) -> datetime:
    """A UTC instant at Manila local `hour:minute` of `day` (Manila = UTC+8)."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)


def build_env(monkeypatch, *routers, extra_tables=()):
    engine = sqlite_helpers.make_engine(*TABLES)
    Session = sqlite_helpers.make_session_factory(engine)
    monkeypatch.setattr(dependencies, "SessionLocal", Session)
    monkeypatch.setattr(staff_directory_cache, "get_by_id", lambda staff_id, **_: STAFF.get(staff_id))
    monkeypatch.setattr(staff_directory_cache, "all_staff", lambda: list(STAFF.values()))
    monkeypatch.setattr(live_sales_order_cache, "find_cached", lambda order_id: None)
    user_cache._cache.clear()
    driver_assignments.reset_cache()
    overview.reset_cache()

    zoho_calls: list[str] = []

    def no_zoho(*args, **kwargs):
        zoho_calls.append("zoho")
        raise AssertionError("an endpoint tried to call Zoho")

    from services import zoho_client

    monkeypatch.setattr(zoho_client, "_request_http", no_zoho)
    monkeypatch.setattr(live_sales_order_cache, "fetch_sales_order_detail", no_zoho)
    monkeypatch.setattr(live_sales_order_cache, "ensure_zoho_data", no_zoho)

    app = FastAPI()
    for router in routers:
        app.include_router(router)

    def get_test_db():
        with Session() as db:
            yield db

    app.dependency_overrides[database.get_db] = get_test_db
    return SimpleNamespace(client=TestClient(app), Session=Session, engine=engine, zoho_calls=zoho_calls, app=app)


def make_user(env, *, name, email, role, staff_id=None, phone=None, status="active"):
    with env.Session() as db:
        user = User(full_name=name, email=email, phone=phone or (639000000000 + zlib.crc32(email.encode()) % 99999999), role=role, status=status, staff_id=staff_id, password_hash=security.unusable_password_hash(), must_change_password=False)
        db.add(user)
        db.commit()
        return user.id


def bearer(user_id, role, email="x@example.test"):
    return {"Authorization": f"Bearer {security.create_access_token(user_id=user_id, role=role, email=email)}"}


def seed_templates(env):
    with env.Session() as db:
        sqlite_helpers.seed_default_templates(db)


# ---- trip data ------------------------------------------------------------------------------------------------------


def add_so(env, so_id, *, number=None, customer=None, batch="batch-1", driver=9, helpers=(), day=None, vehicle="NAA1234", status="assigned", offset=0):
    day = day or today()
    with env.Session() as db:
        db.add(SalesOrderHistory(id=so_id, salesorder_number=number or f"SO-{so_id}", customer_name=customer or f"Customer {so_id}", expected_shipment_date=day, synced_at=datetime.now(timezone.utc),
                                 shipping_address={"address": f"{so_id} Main St", "city": "Makati"}, vehicle_id=vehicle, driver_id=driver, helper_ids=[driver, *helpers],
                                 assigned_at=datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc) + timedelta(minutes=offset), assignment_status=status, assignment_batch_id=batch))
        db.commit()


def add_trip(env, batch="batch-1", *, status="pending", day=None, driver=9, helpers=(), vehicle="NAA1234", warehouse="GLACIER", started=None, ended=None, closed=None):
    with env.Session() as db:
        trip = Trip(batch_id=batch, status=status, delivery_date=day or today(), vehicle=vehicle, driver_staff_id=driver, helper_staff_ids=list(helpers), warehouse=warehouse,
                    started_at=started, ended_at=ended, closed_at=closed, started_gps={"lat": 14.49, "lng": 120.99, "accuracy_m": 8.0, "reason": None} if started else None,
                    ended_gps={"lat": 14.49, "lng": 120.99, "accuracy_m": 9.0, "reason": None} if ended else None)
        db.add(trip)
        db.commit()
        return trip.id


def add_ack(env, trip_id, batch, so_id, *, at, user_id, answers=None, template_id=1, version=1, role="driver", staff_id=9, discrepancy=None):
    answers = answers if answers is not None else [{"item_id": 1, "key": "products_match", "checked": True}, {"item_id": 7, "key": "packs_received", "count": 12}, {"item_id": 9, "key": "remarks", "text": "all good", "note": "short by 0"}]
    with env.Session() as db:
        db.add(SoAcknowledgement(trip_id=trip_id, batch_id=batch, so_id=so_id, template_id=template_id, template_version=version, answers=answers, discrepancy_note=discrepancy, acknowledged_at=at, received_at=at,
                                 by_user_id=user_id, by_role=role, by_staff_id=staff_id, gps={"lat": 14.3, "lng": 121.0, "accuracy_m": 5.0, "reason": None}))
        db.commit()


def add_stop(env, trip_id, so_id, sequence, *, number=None, customer=None, status="pending", checkin=None, checkout=None, outcome=None, reason=None, user_id=None, gps=True, answers=None, distance=None, far=None):
    with env.Session() as db:
        point = {"lat": 14.55 + sequence / 100, "lng": 121.0, "accuracy_m": 10.0, "reason": None} if gps else {"lat": None, "lng": None, "accuracy_m": None, "reason": "denied"}
        db.add(TripStop(trip_id=trip_id, so_id=so_id, sequence=sequence, so_number=number or f"SO-{so_id}", customer_name=customer or f"Customer {so_id}", address_text=f"{so_id} Main St", status=status,
                        checkin_at=checkin, checkin_received_at=checkin, checkin_by_user_id=user_id if checkin else None, checkin_gps=point if checkin else None, checkin_distance_m=distance, checkin_far=far,
                        checkout_at=checkout, checkout_received_at=checkout, checkout_by_user_id=user_id if checkout else None, checkout_gps=point if checkout else None, outcome=outcome, reason=reason,
                        form_template_id=2 if checkout else None, form_template_version=1 if checkout else None, form_answers=answers))
        db.commit()


def add_handover(env, trip_id, batch, items, *, status="pending", officer_user_id=None, submitted=None, user_id=1):
    """items: [(so_id, so_number, customer, handed_over, officer_status)] -> (handover_id, [item ids])."""
    submitted = submitted or datetime.now(timezone.utc)
    with env.Session() as db:
        handover = InvoiceHandover(trip_id=trip_id, batch_id=batch, status=status, submitted_by_user_id=user_id, submitted_at=submitted, submitted_received_at=submitted, officer_user_id=officer_user_id)
        db.add(handover)
        db.flush()
        ids = []
        for so_id, number, customer, handed, officer_status in items:
            item = InvoiceHandoverItem(handover_id=handover.id, so_id=so_id, so_number=number, customer_name=customer, handed_over=handed, officer_status=officer_status)
            db.add(item)
            db.flush()
            ids.append(item.id)
        db.commit()
        return handover.id, ids


def add_event(env, trip_id, batch, event_type, at, *, user_id=1, role="driver", so_id=None, gps=None):
    with env.Session() as db:
        db.add(TripEvent(trip_id=trip_id, batch_id=batch, event_type=event_type, so_id=so_id, user_id=user_id, role=role, client_ts=at, received_at=at, gps=gps))
        db.commit()
