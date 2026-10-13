"""scripts/trip_test_accounts.py: the database guard (pure), argument parsing, seeding and the whole e2e flow, all on in-memory SQLite.
The real backend/.env is never read here (a fake one is written under tmp_path) and no real database is ever opened."""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.pool import StaticPool

os.environ.setdefault("JWT_SECRET", "test-secret-not-real")

from models.sales_order_history import SalesOrderHistory  # noqa: E402
from models.trip import InvoiceHandover, InvoiceHandoverItem, SoAcknowledgement, Trip, TripEvent, TripStop  # noqa: E402
from models.user import User, UserInvite  # noqa: E402
from services import live_sales_order_cache, staff_directory_cache  # noqa: E402
from tests import sqlite_helpers, trip_test_support  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "trip_test_accounts.py")
_spec = importlib.util.spec_from_file_location("trip_test_accounts_script", _SCRIPT)
script = importlib.util.module_from_spec(_spec)
sys.modules["trip_test_accounts_script"] = script
_spec.loader.exec_module(script)

SECRET = "s3cretpw"
PROD_POOLED = f"postgresql://owner:{SECRET}@ep-prod-1-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require"
PROD_DIRECT = f"postgresql://owner:{SECRET}@ep-prod-1.eu-central-1.aws.neon.tech/neondb?sslmode=require"
BRANCH_HOST = "ep-branch-9-pooler.eu-central-1.aws.neon.tech"
BRANCH_URL = f"postgresql://owner:{SECRET}@{BRANCH_HOST}/neondb?sslmode=require"
BRANCH_DIRECT = f"postgresql://owner:{SECRET}@ep-branch-9.eu-central-1.aws.neon.tech/neondb?sslmode=require"
PROD = [PROD_POOLED, PROD_DIRECT]
PASSWORD = "Trip-test-pw-1"


def check(url=BRANCH_URL, *, branch_host=BRANCH_HOST, environ=None, prod=PROD, confirm=True):
    return script.assert_safe_database(url, branch_host=branch_host, environ={} if environ is None else environ, prod_url_candidates=prod, confirm_neon_branch=confirm)


def refused(**kwargs) -> str:
    with pytest.raises(SystemExit) as caught:
        check(**kwargs)
    message = str(caught.value)
    assert message.startswith("REFUSING") and SECRET not in message and "owner" not in message  # never credentials
    return message


# ---- guard: refusals ------------------------------------------------------------------------------------------------------


def test_allows_a_different_neon_branch_with_the_confirm_flag():
    assert check(environ={"DATABASE_URL_DIRECT": BRANCH_DIRECT}) == {"host": BRANCH_HOST, "sqlite": False}


def test_refuses_without_branch_host_or_with_a_different_one():
    assert "--branch-host is required" in refused(branch_host=None)
    assert "--branch-host is required" in refused(branch_host="")
    assert "does not exactly equal" in refused(branch_host="ep-branch-9.eu-central-1.aws.neon.tech")
    assert "does not exactly equal" in refused(branch_host=BRANCH_HOST.upper())


def test_refuses_when_database_url_is_not_exported():
    assert "not exported" in refused(url="")
    assert "not exported" in refused(url=None)


def test_refuses_production_url_from_dotenv_by_equality_and_by_host():
    assert "backend/.env" in refused(url=PROD_POOLED, branch_host="ep-prod-1-pooler.eu-central-1.aws.neon.tech")
    same_host_other_credentials = "postgresql://other:different@ep-prod-1-pooler.eu-central-1.aws.neon.tech/otherdb"
    assert "backend/.env" in refused(url=same_host_other_credentials, branch_host="ep-prod-1-pooler.eu-central-1.aws.neon.tech")
    assert "backend/.env" in refused(url=PROD_DIRECT, branch_host="ep-prod-1.eu-central-1.aws.neon.tech")


def test_refuses_the_pooled_or_direct_twin_of_the_production_endpoint():
    twin = "postgresql://x:y@ep-prod-1.eu-central-1.aws.neon.tech/neondb"  # prod is known by its pooled url only
    assert "backend/.env" in refused(url=twin, branch_host="ep-prod-1.eu-central-1.aws.neon.tech", prod=[PROD_POOLED])


def test_refuses_when_database_url_direct_is_production():
    assert "DATABASE_URL_DIRECT is the same database" in refused(environ={"DATABASE_URL_DIRECT": PROD_DIRECT})
    assert "DATABASE_URL_DIRECT is the same database" in refused(environ={"DATABASE_URL_DIRECT": "postgresql://a:b@ep-prod-1.eu-central-1.aws.neon.tech/other"})


def test_a_url_exported_by_the_shell_that_differs_from_dotenv_passes():
    assert check(url=BRANCH_URL, environ={"DATABASE_URL": BRANCH_URL}, prod=PROD)["host"] == BRANCH_HOST


@pytest.mark.parametrize("name", ["RENDER", "RENDER_SERVICE_ID", "RENDER_INSTANCE_ID"])
def test_refuses_on_render(name):
    assert name in refused(environ={name: "true"})


@pytest.mark.parametrize("name", ["APP_ENV", "ENVIRONMENT"])
def test_refuses_when_the_environment_is_production(name):
    assert name in refused(environ={name: "Production"})
    assert check(environ={name: "staging"})["host"] == BRANCH_HOST


def test_neon_host_needs_the_confirm_flag():
    assert "--confirm-neon-branch" in refused(confirm=False)
    assert "--confirm-neon-branch" in refused(confirm=False, environ={"DATABASE_URL_DIRECT": BRANCH_DIRECT})


def test_all_problems_are_reported_together():
    message = refused(url=PROD_POOLED, branch_host=None, environ={"RENDER": "1", "ENVIRONMENT": "production"}, confirm=False)
    for part in ("--branch-host is required", "backend/.env", "RENDER", "ENVIRONMENT", "--confirm-neon-branch"):
        assert part in message


# ---- guard: allowed cases ---------------------------------------------------------------------------------------------------


def test_sqlite_is_allowed_without_any_flag():
    assert check(url="sqlite://", branch_host=None, confirm=False) == {"host": "", "sqlite": True}
    assert check(url="sqlite:///:memory:", branch_host=None, confirm=False)["sqlite"] is True


@pytest.mark.parametrize("url,host", [("postgresql://u:p@localhost:5432/db", "localhost"), ("postgresql://u:p@127.0.0.1:5432/db", "127.0.0.1"), ("postgresql://u:p@[::1]:5432/db", "::1")])
def test_local_hosts_are_allowed_without_the_neon_flag(url, host):
    assert check(url=url, branch_host=host, confirm=False)["host"] == host


def test_local_hosts_still_need_a_matching_branch_host():
    assert "--branch-host is required" in refused(url="postgresql://u:p@localhost/db", branch_host=None, confirm=False)


def test_read_env_file_urls_reads_a_fake_file_without_touching_os_environ(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={PROD_POOLED}\nDATABASE_URL_DIRECT={PROD_DIRECT}\nOTHER=1\n", encoding="utf-8")
    before = dict(os.environ)
    assert script.read_env_file_urls(str(env_file)) == [PROD_POOLED, PROD_DIRECT]
    assert dict(os.environ) == before
    assert script.read_env_file_urls(str(tmp_path / "missing.env")) == []
    assert "backend" in script.ENV_PATH.replace("\\", "/") and script.ENV_PATH.endswith(".env")


def test_main_refuses_before_importing_anything_heavy(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={BRANCH_URL}\n", encoding="utf-8")  # fake: the "production" url of this test
    monkeypatch.setattr(script, "ENV_PATH", str(env_file))
    monkeypatch.setenv("DATABASE_URL", BRANCH_URL)
    with pytest.raises(SystemExit) as caught:
        script.main(["seed", "--branch-host", BRANCH_HOST, "--confirm-neon-branch"])
    assert "backend/.env" in str(caught.value) and SECRET not in str(caught.value)
    with pytest.raises(SystemExit) as caught:
        script.main(["e2e", "--password", PASSWORD])  # no --branch-host
    assert "--branch-host is required" in str(caught.value)


def test_main_refuses_a_different_branch_when_the_engine_host_would_differ(tmp_path, monkeypatch):
    monkeypatch.setattr(script, "ENV_PATH", str(tmp_path / "nothing.env"))
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")  # allowed host, but not what conftest's engine points at
    with pytest.raises(SystemExit) as caught:
        script.main(["seed", "--branch-host", "localhost"])
    assert "engine host differs" in str(caught.value)


# ---- schema head ------------------------------------------------------------------------------------------------------------


def _engine_with_version(version):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    if version is not None:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": version})
    return engine


def test_schema_at_head_passes_and_behind_or_missing_is_refused_with_the_upgrade_hint():
    head = "q7c8d9e0f1a2"
    assert script.assert_schema_at_head(_engine_with_version(head), head=head) == head
    for engine in (_engine_with_version("o5a6b7c8d9e0"), _engine_with_version(None)):
        with pytest.raises(SystemExit) as caught:
            script.assert_schema_at_head(engine, head=head)
        assert "python -m alembic upgrade head" in str(caught.value)


def test_schema_head_is_read_from_the_alembic_scripts():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    expected = ScriptDirectory.from_config(Config(script.ALEMBIC_INI)).get_current_head()
    assert script.assert_schema_at_head(_engine_with_version(expected)) == expected


# ---- argument parsing -------------------------------------------------------------------------------------------------------


def test_seed_arguments_and_defaults():
    args = script.build_parser().parse_args(["seed"])
    assert (args.driver_staff_id, args.helper_staff_id, args.apply, args.force_change, args.with_invite, args.branch_host, args.confirm_neon_branch) == (9, 17, False, False, False, None, False)
    args = script.build_parser().parse_args(["seed", "--branch-host", "h", "--confirm-neon-branch", "--apply", "--force-change", "--with-invite", "--password", "pw", "--driver-staff-id", "10", "--helper-staff-id", "18"])
    assert (args.branch_host, args.confirm_neon_branch, args.apply, args.force_change, args.with_invite, args.password, args.driver_staff_id, args.helper_staff_id) == ("h", True, True, True, True, "pw", 10, 18)


def test_e2e_and_cleanup_arguments(monkeypatch):
    monkeypatch.delenv("TRIP_TEST_PASSWORD", raising=False)
    args = script.build_parser().parse_args(["e2e", "--branch-host", "h", "--cleanup", "--cleanup-users", "--orders", "5", "--password", "pw"])
    assert (args.cleanup, args.cleanup_users, args.orders, args.password) == (True, True, 5, "pw")
    assert script.build_parser().parse_args(["e2e"]).orders == 3
    assert script.build_parser().parse_args(["cleanup", "--batch-id", "TRIPE2E-BATCH-1"]).batch_id == "TRIPE2E-BATCH-1"
    for bad in (["cleanup"], [], ["unknown"]):
        with pytest.raises(SystemExit):
            script.build_parser().parse_args(bad)


def test_main_validates_e2e_inputs_before_the_guard(monkeypatch):
    monkeypatch.delenv("TRIP_TEST_PASSWORD", raising=False)
    with pytest.raises(SystemExit) as caught:
        script.main(["e2e", "--branch-host", "h"])
    assert "password" in str(caught.value)
    with pytest.raises(SystemExit) as caught:
        script.main(["e2e", "--password", "pw", "--orders", "0"])
    assert "--orders" in str(caught.value)


# ---- seeding + e2e on in-memory SQLite --------------------------------------------------------------------------------------


@pytest.fixture()
def db_env():
    engine = sqlite_helpers.make_engine(*trip_test_support.TABLES)
    Session = sqlite_helpers.make_session_factory(engine)
    with Session() as db:
        sqlite_helpers.seed_default_templates(db)
    yield Session
    engine.dispose()


def users_by_email(Session):
    with Session() as db:
        return {u.email: u for u in db.execute(select(User)).scalars().all()}


def test_seed_is_dry_run_by_default_and_writes_nothing(db_env):
    lines: list[str] = []
    result = script.seed_accounts(db_env, apply=False, emit=lines.append)
    assert users_by_email(db_env) == {} and len(result.created) == 3 and result.generated_password is None
    assert any("dry run" in line for line in lines)


def test_seed_apply_creates_three_users_with_philippine_phones_and_is_idempotent(db_env):
    from auth.security import verify_password
    from services.phone_numbers import ph_phone_digits

    lines: list[str] = []
    result = script.seed_accounts(db_env, apply=True, emit=lines.append)
    users = users_by_email(db_env)
    assert sorted(users) == ["trip-test-driver@rgf.test", "trip-test-helper@rgf.test", "trip-test-officer@rgf.test"]
    driver, helper, officer = (users[f"trip-test-{k}@rgf.test"] for k in ("driver", "helper", "officer"))
    assert (driver.role, driver.staff_id, helper.role, helper.staff_id, officer.role, officer.staff_id) == ("driver", 9, "helper", 17, "logistics_officer", None)
    assert len({driver.phone, helper.phone, officer.phone}) == 3 and all(ph_phone_digits(f"0{str(u.phone)[2:]}") == u.phone for u in users.values())
    assert all(u.status == "active" and u.must_change_password is False for u in users.values())
    assert result.generated_password and all(verify_password(result.generated_password, u.password_hash) for u in users.values())
    assert sum(result.generated_password in line for line in lines) == 1  # printed once
    again: list[str] = []
    second = script.seed_accounts(db_env, apply=True, emit=again.append)
    assert second.created == [] and len(second.existing) == 3 and second.generated_password is None and len(users_by_email(db_env)) == 3
    assert not any(result.generated_password in line for line in again)


def test_seed_password_force_change_and_invites(db_env):
    from auth.security import verify_password

    lines: list[str] = []
    result = script.seed_accounts(db_env, apply=True, password=PASSWORD, force_change=True, with_invite=True, driver_staff_id=22, helper_staff_id=23, emit=lines.append)
    users = users_by_email(db_env)
    assert result.generated_password is None and all(u.must_change_password is True and verify_password(PASSWORD, u.password_hash) for u in users.values())
    assert users["trip-test-driver@rgf.test"].staff_id == 22 and users["trip-test-helper@rgf.test"].staff_id == 23
    assert sorted(result.invite_paths) == ["driver", "helper", "officer"] and all(p.startswith("/set-password?token=") for p in result.invite_paths.values())
    assert not any(PASSWORD in line for line in lines)
    with db_env() as db:
        assert db.scalar(select(func.count()).select_from(UserInvite)) == 3


def test_seed_reports_a_staff_link_clash_instead_of_overwriting(db_env):
    with db_env() as db:
        db.add(User(full_name="Real Driver", email="real@example.test", phone=639171112222, role="driver", status="active", staff_id=9, password_hash="x"))
        db.commit()
    result = script.seed_accounts(db_env, apply=True, password=PASSWORD, emit=lambda _line: None)
    assert result.problems and "driver" in result.problems[0] and sorted(result.created) == ["helper", "officer"]
    assert "trip-test-driver@rgf.test" not in users_by_email(db_env)


def test_seed_rejects_a_weak_password(db_env):
    with pytest.raises(SystemExit):
        script.seed_accounts(db_env, apply=True, password="short", emit=lambda _line: None)


def counts(Session):
    with Session() as db:
        return {m.__tablename__: db.scalar(select(func.count()).select_from(m)) for m in (Trip, SoAcknowledgement, TripStop, TripEvent, InvoiceHandover, InvoiceHandoverItem, SalesOrderHistory)}


def test_full_e2e_flow_on_sqlite_leaves_a_closed_trip_and_blocks_external_calls(db_env):
    script.seed_accounts(db_env, apply=True, password=PASSWORD, emit=lambda _line: None)
    staff_before = list(staff_directory_cache._staff)
    lines: list[str] = []
    report = script.run_trip_e2e(db_env, password=PASSWORD, n_orders=3, emit=lines.append)
    assert report.ok, report.table()
    names = [name for name, _, _ in report.rows]
    for expected in ("driver logs in with phone number", "helper repeating the driver's acknowledge -> already_done", "helper repeating start -> already_done (not 409)",
                     "replaying an acknowledge with the same idempotency_key returns replayed=true", "driver ends the trip", "trip status is closed (database)"):
        assert expected in names
    table = report.table()
    assert "PASS" in table and "FAIL" not in table and "0 failed" in table
    # the run really closed everything, and the fake rows are still there because cleanup was not requested
    with db_env() as db:
        trip = db.execute(select(Trip)).scalar_one()
        handover = db.execute(select(InvoiceHandover)).scalar_one()
        assert trip.status == "closed" and handover.status == "closed" and trip.batch_id.startswith("TRIPE2E-BATCH-")
        assert all(row.id.startswith("TRIPE2E-SO-") and row.assignment_batch_id == trip.batch_id for row in db.execute(select(SalesOrderHistory)).scalars().all())
    assert counts(db_env) == {"trips": 1, "so_acknowledgements": 3, "trip_stops": 3, "trip_events": 17, "invoice_handovers": 1, "invoice_handover_items": 3, "sales_orders": 3}
    assert any("cleanup --batch-id TRIPE2E-BATCH-" in line for line in lines)
    # in-memory state restored: staff directory and the live cache hold none of the fakes
    assert staff_directory_cache._staff == staff_before
    assert not [k for k in live_sales_order_cache._assignment_state if k.startswith("TRIPE2E-")]
    assert not [k for k in live_sales_order_cache._assigned_zoho if k.startswith("TRIPE2E-")]


def test_e2e_cleanup_removes_only_its_own_rows(db_env):
    script.seed_accounts(db_env, apply=True, password=PASSWORD, emit=lambda _line: None)
    # an unrelated real-looking assignment that must survive the cleanup
    trip_test_support_env = type("E", (), {"Session": db_env})
    trip_test_support.add_so(trip_test_support_env, "real-so-1", batch="real-batch", driver=10, helpers=(), number="SO-REAL-1")
    trip_id = trip_test_support.add_trip(trip_test_support_env, "real-batch", driver=10)
    before = counts(db_env)
    report = script.run_trip_e2e(db_env, password=PASSWORD, n_orders=2, cleanup=True, emit=lambda _line: None)
    assert report.ok, report.table()
    assert any(name.startswith("cleanup removed") and ok for name, ok, _ in report.rows)
    assert counts(db_env) == before  # only the real rows remain
    with db_env() as db:
        assert db.get(Trip, trip_id) is not None and db.get(SalesOrderHistory, "real-so-1") is not None
    assert len(users_by_email(db_env)) == 3  # users stay without --cleanup-users


def test_e2e_cleanup_users_removes_the_test_users_too(db_env):
    script.seed_accounts(db_env, apply=True, with_invite=True, password=PASSWORD, emit=lambda _line: None)
    report = script.run_trip_e2e(db_env, password=PASSWORD, n_orders=1, cleanup=True, cleanup_users=True, emit=lambda _line: None)
    assert report.ok, report.table()
    assert users_by_email(db_env) == {}
    with db_env() as db:
        assert db.scalar(select(func.count()).select_from(UserInvite)) == 0


def test_e2e_fails_cleanly_when_users_are_not_seeded(db_env):
    report = script.run_trip_e2e(db_env, password=PASSWORD, n_orders=2, emit=lambda _line: None)
    assert not report.ok and report.rows[0][1] is False and "seed" in report.rows[0][0]
    assert counts(db_env)["sales_orders"] == 0 and counts(db_env)["trips"] == 0


def test_e2e_reports_a_wrong_password_as_a_failed_step(db_env):
    script.seed_accounts(db_env, apply=True, password=PASSWORD, emit=lambda _line: None)
    report = script.run_trip_e2e(db_env, password="not-the-password", n_orders=1, cleanup=True, emit=lambda _line: None)
    assert not report.ok and any(name == "driver logs in with phone number" and not ok for name, ok, _ in report.rows)
    assert counts(db_env)["sales_orders"] == 0  # cleanup ran even though a step failed


def test_cleanup_refuses_ids_that_are_not_tripe2e_batches(db_env):
    for bad in ("batch-1", "", "TRIPE2E-BATCH-", "TRIPE2E-SO-1"):
        with pytest.raises(SystemExit):
            script.cleanup_batch(db_env, bad)


def test_answer_builders_follow_the_active_templates(db_env):
    from services import form_templates

    with db_env() as db:
        ack = form_templates.load_active(db, "acknowledge_checklist")
        checkout = form_templates.load_active(db, "delivery_checkout")
    answers = script.build_ack_answers(ack)
    assert {a["item_id"] for a in answers if a.get("checked")} == {i["id"] for i in ack["items"] if i["item_type"] == "checkbox"}
    assert script.build_checkout_answers(checkout) == {"outcome": "Delivered", "received_by": "TRIPE2E test"}
    failed = script.build_checkout_answers(checkout, "Failed / Returned")
    assert failed == {"outcome": "Failed / Returned", "reason": "TRIPE2E test"}
    assert form_templates.validate_checkout(checkout, failed, False, None, {"so_number": "x", "customer": "x", "delivery_address": "x", "delivery_date": "2026-10-12", "driver": "x", "vehicle": "x"})[1] == []
