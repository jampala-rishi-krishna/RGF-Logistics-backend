"""In-memory SQLite stand-in for the trip-workflow tests (Postgres-only column types are mapped to SQLite ones).

The test suite must never open the real database (conftest.py). Tests that need real tables build their own
SQLite engine with `make_engine()` and monkeypatch `auth.dependencies.SessionLocal` / override `database.get_db`.
"""
from __future__ import annotations

from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


def make_engine(*tables):
    """Engine with exactly `tables` (model classes' __table__ objects, in dependency order) created."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    for table in tables:
        table.create(engine)
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def seed_default_templates(db) -> None:
    """The same two DRAFT templates the migration seeds (acknowledge checklist v1, delivery checkout v1), for tests."""
    from importlib import import_module

    from models.trip import FormTemplate, FormTemplateItem

    migration = import_module("migrations.versions.o5a6b7c8d9e0_trip_workflow")
    db.add(FormTemplate(id=1, kind="acknowledge_checklist", version=1, name="DRAFT — replace with Logistics checklist", is_draft=True, active=True))
    db.add(FormTemplate(id=2, kind="delivery_checkout", version=1, name="DRAFT — mirror of Zoho form", is_draft=True, active=True))
    db.flush()
    for position, (key, label, item_type, required) in enumerate(migration.ACK_ITEMS, 1):
        db.add(FormTemplateItem(template_id=1, position=position, key=key, label=label, item_type=item_type, required=required, active=True))
    for position, (key, label, item_type, required, options, prefill, show_when) in enumerate(migration.CHECKOUT_ITEMS, 1):
        db.add(FormTemplateItem(template_id=2, position=position, key=key, label=label, item_type=item_type, required=required, options=options, prefill=prefill, show_when=show_when, active=True))
    db.commit()
