"""Admin-editable settings: precedence (admin > env > default), validation, the secrets whitelist, the single startup read,
write-only-on-save, the admin API, and that the four consumers actually follow the setting. In-memory SQLite only."""
from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("JWT_SECRET", "test-secret-not-real")

import database  # noqa: E402
from auth import dependencies, security  # noqa: E402
from models.app_setting import AppSetting  # noqa: E402
from models.user import User  # noqa: E402
from routers import admin, driver as driver_router  # noqa: E402
from services import app_settings, overview, user_cache, zoho_usage  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    for table in (User.__table__, AppSetting.__table__):
        table.create(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(dependencies, "SessionLocal", Session)
    for name in ("DISPATCH_PHONE", "DRIVER_STALE_MINUTES", "ZOHO_DAILY_BUDGET", "ZOHO_FAILSAFE_SO_DETAIL_CAP", "ZOHO_ORG_DAILY_LIMIT"):
        monkeypatch.delenv(name, raising=False)
    user_cache._cache.clear()
    app_settings.reset()

    app = FastAPI()
    app.include_router(admin.usage_router)

    def get_test_db():
        with Session() as db:
            yield db

    app.dependency_overrides[database.get_db] = get_test_db
    with Session() as db:
        admin_user = User(full_name="Admin", email="admin@example.test", phone=639170000001, role="admin", status="active",
                          password_hash=security.hash_password("pw-for-tests-only"), must_change_password=False)
        dispatcher = User(full_name="Dispatcher", email="dispatcher@example.test", phone=639170000002, role="dispatcher", status="active",
                          password_hash=security.hash_password("pw-for-tests-only"), must_change_password=False)
        db.add_all([admin_user, dispatcher])
        db.commit()
        ids = {"admin": (admin_user.id, "admin@example.test"), "dispatcher": (dispatcher.id, "dispatcher@example.test")}

    def headers(role):
        uid, email = ids[role]
        return {"Authorization": f"Bearer {security.create_access_token(user_id=uid, role=role, email=email)}"}

    yield SimpleNamespace(client=TestClient(app), Session=Session, headers=headers)
    app_settings.reset()
    engine.dispose()


def rows(env):
    with env.Session() as db:
        return {r.key: (r.value, r.updated_by) for r in db.execute(select(AppSetting)).scalars().all()}


# --- precedence ------------------------------------------------------------------------------------------------------

def test_defaults_when_nothing_is_set(env):
    assert app_settings.get("DISPATCH_PHONE") == ""
    assert app_settings.get("DRIVER_STALE_MINUTES") == 15
    assert app_settings.get("ZOHO_DAILY_BUDGET") == 4500
    assert app_settings.get("ZOHO_FAILSAFE_SO_DETAIL_CAP") == 50


def test_env_beats_default_and_admin_beats_env(env, monkeypatch):
    monkeypatch.setenv("DRIVER_STALE_MINUTES", "25")
    assert app_settings.get("DRIVER_STALE_MINUTES") == 25
    with env.Session() as db:
        app_settings.save(db, "DRIVER_STALE_MINUTES", 40, "admin@example.test")
    assert app_settings.get("DRIVER_STALE_MINUTES") == 40
    monkeypatch.setenv("DRIVER_STALE_MINUTES", "99")  # env changes no longer matter while an override exists
    assert app_settings.get("DRIVER_STALE_MINUTES") == 40
    with env.Session() as db:
        app_settings.clear(db, "DRIVER_STALE_MINUTES")
    assert app_settings.get("DRIVER_STALE_MINUTES") == 99  # back to the environment value


def test_invalid_env_value_falls_back_to_the_default(env, monkeypatch):
    monkeypatch.setenv("ZOHO_DAILY_BUDGET", "not-a-number")
    assert app_settings.get("ZOHO_DAILY_BUDGET") == 4500


# --- validation ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key,value", [
    ("DRIVER_STALE_MINUTES", 0), ("DRIVER_STALE_MINUTES", 1441), ("DRIVER_STALE_MINUTES", "abc"), ("DRIVER_STALE_MINUTES", ""), ("DRIVER_STALE_MINUTES", None),
    ("DRIVER_STALE_MINUTES", True), ("ZOHO_DAILY_BUDGET", 99), ("ZOHO_DAILY_BUDGET", 10001), ("ZOHO_FAILSAFE_SO_DETAIL_CAP", -1),
    ("ZOHO_FAILSAFE_SO_DETAIL_CAP", 1001), ("DISPATCH_PHONE", "call me maybe"), ("DISPATCH_PHONE", "1" * 33),
])
def test_invalid_values_are_rejected_and_nothing_is_written(env, key, value):
    with env.Session() as db, pytest.raises(app_settings.SettingError):
        app_settings.save(db, key, value, "admin@example.test")
    assert rows(env) == {}


def test_budget_cannot_exceed_the_organisation_limit(env, monkeypatch):
    monkeypatch.setenv("ZOHO_ORG_DAILY_LIMIT", "6000")
    with env.Session() as db:
        assert app_settings.save(db, "ZOHO_DAILY_BUDGET", 6000, "a")["value"] == 6000
        with pytest.raises(app_settings.SettingError):
            app_settings.save(db, "ZOHO_DAILY_BUDGET", 6001, "a")


def test_phone_may_be_cleared_to_hide_the_call_button(env):
    with env.Session() as db:
        assert app_settings.save(db, "DISPATCH_PHONE", "+63 917 000 0000", "a")["value"] == "+63 917 000 0000"
        assert app_settings.save(db, "DISPATCH_PHONE", "", "a")["value"] == ""
    assert rows(env)["DISPATCH_PHONE"][0] == ""


# --- secrets can never be stored -------------------------------------------------------------------------------------

SECRET_WORDS = re.compile(r"SECRET|TOKEN|PASSWORD|API_KEY|ACCESS_KEY|R2_|JWT|DATABASE|CLIENT_ID|REFRESH|CARTRACK|GMAIL|WEBHOOK", re.I)


def test_the_whitelist_contains_no_secret_names():
    assert sorted(app_settings.DEFINITIONS) == ["DISPATCH_PHONE", "DRIVER_STALE_MINUTES", "ZOHO_DAILY_BUDGET", "ZOHO_FAILSAFE_SO_DETAIL_CAP"]
    assert [k for k in app_settings.DEFINITIONS if SECRET_WORDS.search(k)] == []


@pytest.mark.parametrize("key", ["R2_SECRET_ACCESS_KEY", "JWT_SECRET", "DATABASE_URL", "ZOHO_REFRESH_TOKEN", "GMAIL_COMMS_REFRESH_TOKEN", "CARTRACK_API_KEY", "anything"])
def test_secret_and_unknown_keys_are_refused_everywhere(env, key):
    with env.Session() as db, pytest.raises(KeyError):
        app_settings.save(db, key, "x", "a")
    assert env.client.put(f"/api/admin/settings/{key}", json={"value": "x"}, headers=env.headers("admin")).status_code == 404
    assert env.client.delete(f"/api/admin/settings/{key}", headers=env.headers("admin")).status_code == 404
    assert rows(env) == {}


# --- single startup read, write only on save -------------------------------------------------------------------------

def test_load_reads_once_and_skips_unknown_or_invalid_rows(env):
    with env.Session() as db:
        db.add_all([AppSetting(key="DRIVER_STALE_MINUTES", value="30", updated_by="x"), AppSetting(key="ZOHO_DAILY_BUDGET", value="5", updated_by="x"),
                    AppSetting(key="JWT_SECRET", value="nope", updated_by="x")])
        db.commit()
    assert app_settings.load(env.Session) is True
    assert app_settings.get("DRIVER_STALE_MINUTES") == 30
    assert app_settings.get("ZOHO_DAILY_BUDGET") == 4500  # invalid stored value ignored
    assert "JWT_SECRET" not in app_settings._overrides


def test_load_failure_keeps_environment_values(env, monkeypatch):
    monkeypatch.setenv("DRIVER_STALE_MINUTES", "22")

    def broken():
        raise RuntimeError("database down")

    assert app_settings.load(broken) is False
    assert app_settings.get("DRIVER_STALE_MINUTES") == 22


def test_reads_never_touch_the_database(env, monkeypatch):
    with env.Session() as db:
        app_settings.save(db, "ZOHO_DAILY_BUDGET", 5000, "a")
    import sqlalchemy.orm as orm

    def forbidden(*args, **kwargs):
        raise AssertionError("a settings read touched the database")

    for name in ("execute", "get", "scalars", "scalar", "query", "commit", "add", "delete"):
        monkeypatch.setattr(orm.Session, name, forbidden)
    for _ in range(1000):
        assert app_settings.get_int("ZOHO_DAILY_BUDGET") == 5000
        assert zoho_usage.budget() == 5000
    assert len(app_settings.describe()) == 4
    assert app_settings.get_str("DISPATCH_PHONE") == ""


# --- consumers follow the setting ------------------------------------------------------------------------------------

def test_zoho_budget_and_failsafe_cap_follow_the_setting(env, monkeypatch):
    monkeypatch.setenv("ZOHO_DAILY_BUDGET", "4000")
    assert zoho_usage.budget() == 4000
    with env.Session() as db:
        app_settings.save(db, "ZOHO_DAILY_BUDGET", 5200, "a")
        app_settings.save(db, "ZOHO_FAILSAFE_SO_DETAIL_CAP", 7, "a")
    assert zoho_usage.budget() == 5200
    assert zoho_usage.failsafe_so_detail_cap() == 7


def test_stale_minutes_and_dispatch_phone_follow_the_setting(env, monkeypatch):
    monkeypatch.setenv("DISPATCH_PHONE", "+639170000000")
    assert driver_router._dispatch_phone() == "+639170000000"
    with env.Session() as db:
        app_settings.save(db, "DISPATCH_PHONE", "+639180000000", "a")
        app_settings.save(db, "DRIVER_STALE_MINUTES", 45, "a")
        assert overview.stale_minutes_default() == 45
        assert driver_router._dispatch_phone() == "+639180000000"
        app_settings.save(db, "DISPATCH_PHONE", "", "a")
        assert driver_router._dispatch_phone() is None  # empty override hides the Call button even though env has a number


# --- admin API -------------------------------------------------------------------------------------------------------

def test_admin_lists_effective_values_with_their_source(env, monkeypatch):
    monkeypatch.setenv("DRIVER_STALE_MINUTES", "20")
    body = env.client.get("/api/admin/settings", headers=env.headers("admin")).json()["settings"]
    by_key = {s["key"]: s for s in body}
    assert by_key["DRIVER_STALE_MINUTES"]["value"] == 20 and by_key["DRIVER_STALE_MINUTES"]["source"] == "env"
    assert by_key["ZOHO_DAILY_BUDGET"]["source"] == "default" and by_key["ZOHO_DAILY_BUDGET"]["max"] == 10000
    assert by_key["DISPATCH_PHONE"]["type"] == "text" and by_key["DRIVER_STALE_MINUTES"]["type"] == "number"


def test_admin_save_writes_one_row_with_who_and_when_and_applies_immediately(env):
    r = env.client.put("/api/admin/settings/ZOHO_DAILY_BUDGET", json={"value": 5000}, headers=env.headers("admin"))
    assert r.status_code == 200, r.text
    assert r.json()["value"] == 5000 and r.json()["source"] == "admin" and r.json()["updated_by"] == "admin@example.test"
    assert rows(env) == {"ZOHO_DAILY_BUDGET": ("5000", "admin@example.test")}
    assert zoho_usage.budget() == 5000


def test_admin_save_overwrites_the_same_row_not_adds(env):
    for value in (4600, 4700, 4800):
        assert env.client.put("/api/admin/settings/ZOHO_DAILY_BUDGET", json={"value": value}, headers=env.headers("admin")).status_code == 200
    assert rows(env) == {"ZOHO_DAILY_BUDGET": ("4800", "admin@example.test")}


def test_admin_save_rejects_bad_values_with_a_readable_message(env):
    r = env.client.put("/api/admin/settings/DRIVER_STALE_MINUTES", json={"value": 0}, headers=env.headers("admin"))
    assert r.status_code == 422 and "at least 1" in r.json()["detail"]
    assert rows(env) == {}


def test_admin_reset_removes_the_override(env, monkeypatch):
    monkeypatch.setenv("ZOHO_DAILY_BUDGET", "4100")
    env.client.put("/api/admin/settings/ZOHO_DAILY_BUDGET", json={"value": 5000}, headers=env.headers("admin"))
    r = env.client.delete("/api/admin/settings/ZOHO_DAILY_BUDGET", headers=env.headers("admin"))
    assert r.status_code == 200 and r.json()["value"] == 4100 and r.json()["source"] == "env"
    assert rows(env) == {}


def test_only_admins_can_read_or_change_settings(env):
    for role in ("dispatcher",):
        assert env.client.get("/api/admin/settings", headers=env.headers(role)).status_code == 403
        assert env.client.put("/api/admin/settings/ZOHO_DAILY_BUDGET", json={"value": 5000}, headers=env.headers(role)).status_code == 403
        assert env.client.delete("/api/admin/settings/ZOHO_DAILY_BUDGET", headers=env.headers(role)).status_code == 403
    assert env.client.get("/api/admin/settings").status_code in (401, 403)
    assert rows(env) == {}


def test_changes_are_audited_without_touching_the_database_again(env):
    from services import audit

    env.client.put("/api/admin/settings/DRIVER_STALE_MINUTES", json={"value": 30}, headers=env.headers("admin"))
    entries = [e for e in audit.list_audit_log("app_setting")]
    assert entries and entries[-1]["action"] == "setting_changed" and entries[-1]["details"] == {"from": 15, "to": 30}


def test_environment_fallback_stays_as_permissive_as_the_old_env_reads(env, monkeypatch):
    """Only the admin form enforces the tighter bounds; ops/tests may still run with tiny budgets via env."""
    monkeypatch.setenv("ZOHO_DAILY_BUDGET", "10")
    monkeypatch.setenv("ZOHO_FAILSAFE_SO_DETAIL_CAP", "0")
    assert zoho_usage.budget() == 10 and zoho_usage.failsafe_so_detail_cap() == 0
