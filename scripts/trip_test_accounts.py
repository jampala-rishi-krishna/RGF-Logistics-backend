"""Seed test logins and run an end-to-end driver/helper trip check against a THROWAWAY database (a Neon branch).

    seed     create 3 test users (driver, helper, logistics officer). DRY RUN unless --apply.
    e2e      drive the whole trip flow in-process (login -> acknowledge -> start -> check-in/out -> end -> handover ->
             officer validates -> closed) with fake TRIPE2E- Sales Orders. No Zoho, no n8n, no network except the database.
    cleanup  remove the rows of one earlier e2e run (by its TRIPE2E-BATCH- id).

PowerShell, from backend/ (Neon branch; take the host from the branch's connection string):

    $env:DATABASE_URL="<branch pooled url>"; $env:DATABASE_URL_DIRECT="<branch direct url>"
    python scripts/trip_test_accounts.py seed --branch-host <host-of-DATABASE_URL> --confirm-neon-branch --password "<pw>" --apply
    python scripts/trip_test_accounts.py e2e  --branch-host <host-of-DATABASE_URL> --confirm-neon-branch --password "<pw>" --cleanup
    # optional: --with-invite on seed prints a set-password path per user; --force-change sets must_change_password;
    #           --cleanup-users on e2e/cleanup also deletes the three test users; --orders N sets the SO count (default 3)

DATABASE_URL must be EXPORTED in the shell. backend/.env points at PRODUCTION on this machine; database.py calls
load_dotenv(), which never overrides a variable that is already set, so the exported branch URL wins. This script
refuses to start when DATABASE_URL is not exported (it will not fall back to backend/.env).

Safety (assert_safe_database, runs BEFORE database / models are imported, so nothing is connected or written when it refuses):
  - --branch-host must equal the host of DATABASE_URL exactly;
  - DATABASE_URL / DATABASE_URL_DIRECT must not equal or share a host with the ones in backend/.env (read, never loaded or printed);
  - refuses when RENDER / RENDER_SERVICE_ID / RENDER_INSTANCE_ID is set, or APP_ENV / ENVIRONMENT is production;
  - a neon.tech host needs --confirm-neon-branch;
  - the schema must be at alembic head (otherwise: python -m alembic upgrade head).
sqlite URLs and localhost / 127.0.0.1 / ::1 are allowed without the Neon flag (that is how the tests run it).
Only existing models / existing code paths write rows; no table, column or write path is added.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BACKEND_DIR, ".env")
ALEMBIC_INI = os.path.join(BACKEND_DIR, "alembic.ini")

RENDER_VARS = ("RENDER", "RENDER_SERVICE_ID", "RENDER_INSTANCE_ID")
ENV_NAME_VARS = ("APP_ENV", "ENVIRONMENT")
PRODUCTION_NAMES = ("production", "prod")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
UPGRADE_HINT = "python -m alembic upgrade head"

FAKE_PREFIX = "TRIPE2E-"
BATCH_PREFIX = "TRIPE2E-BATCH-"
DEFAULT_DRIVER_STAFF_ID = 9
DEFAULT_HELPER_STAFF_ID = 17


# ---------------------------------------------------------------------------------------------------------------------
# Database guard (pure: no imports of database / models, no connection)
# ---------------------------------------------------------------------------------------------------------------------


def url_host(url: str | None) -> str:
    """Lower-case host of a database URL ('' for sqlite / unparsable). Never returns credentials."""
    try:
        return (urlsplit(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""


def _endpoint_key(host: str) -> str:
    """Neon pooled and direct hosts differ only by '-pooler' on the first label: treat them as the same endpoint."""
    label, _, rest = host.partition(".")
    return f"{label[:-len('-pooler')] if label.endswith('-pooler') else label}.{rest}" if host else ""


def _is_sqlite(url: str) -> bool:
    return str(url or "").strip().lower().startswith("sqlite")


def _same_database(url: str, candidate: str) -> bool:
    if not candidate:
        return False
    if str(url).strip() == str(candidate).strip():
        return True
    host, other = url_host(url), url_host(candidate)
    return bool(host and other and (host == other or _endpoint_key(host) == _endpoint_key(other)))


def read_env_file_urls(env_path: str = ENV_PATH) -> list[str]:
    """DATABASE_URL / DATABASE_URL_DIRECT as written in backend/.env (production on this machine). Read with
    dotenv_values: nothing is put into os.environ and nothing is printed."""
    from dotenv import dotenv_values

    try:
        values = dotenv_values(env_path)
    except OSError:
        return []
    return [str(values[name]).strip() for name in ("DATABASE_URL", "DATABASE_URL_DIRECT") if values.get(name) and str(values[name]).strip()]


def assert_safe_database(url, *, branch_host, environ, prod_url_candidates, confirm_neon_branch: bool = False) -> dict:
    """Raise SystemExit (clear message, credentials never shown) unless `url` is safe to write test data to.
    `environ` is a mapping (os.environ); `prod_url_candidates` are the URLs found in backend/.env;
    `confirm_neon_branch` is the --confirm-neon-branch flag. Returns {"host", "sqlite"}."""
    url = str(url or "").strip()
    host = url_host(url)
    sqlite = _is_sqlite(url)
    direct = str(environ.get("DATABASE_URL_DIRECT") or "").strip()
    candidates = [c for c in (prod_url_candidates or []) if c]
    problems: list[str] = []

    if not url:
        problems.append("DATABASE_URL is not exported in this shell. Export the Neon BRANCH url first; this script never falls back to backend/.env.")
    elif not sqlite:
        if not host:
            problems.append("DATABASE_URL has no host.")
        if not branch_host:
            problems.append("--branch-host is required: type the host of the Neon branch you mean to use.")
        elif branch_host != host:
            problems.append(f"--branch-host ({branch_host}) does not exactly equal the host of DATABASE_URL ({host or 'none'}).")
    for label, value in (("DATABASE_URL", url), ("DATABASE_URL_DIRECT", direct)):
        if value and any(_same_database(value, candidate) for candidate in candidates):
            problems.append(f"{label} is the same database (or host) as the one in backend/.env, which is PRODUCTION on this machine.")
    for name in RENDER_VARS:
        if str(environ.get(name) or "").strip():
            problems.append(f"{name} is set: this looks like a Render deployment, not a developer shell.")
    for name in ENV_NAME_VARS:
        if str(environ.get(name) or "").strip().lower() in PRODUCTION_NAMES:
            problems.append(f"{name} is set to production.")
    for label, value in (("DATABASE_URL", url), ("DATABASE_URL_DIRECT", direct)):
        value_host = url_host(value)
        if value_host and "neon.tech" in value_host and not confirm_neon_branch:
            problems.append(f"{label} is a Neon host ({value_host}) and --confirm-neon-branch was not passed.")
    if problems:
        raise SystemExit("REFUSING to run (nothing was connected or written):\n  - " + "\n  - ".join(dict.fromkeys(problems)))
    return {"host": host, "sqlite": sqlite}


def assert_schema_at_head(engine, *, ini_path: str = ALEMBIC_INI, head: str | None = None) -> str:
    """alembic_version must hold exactly the single head revision, else SystemExit with the upgrade hint."""
    from sqlalchemy import text

    if head is None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(Config(ini_path)).get_current_head()
    try:
        with engine.connect() as conn:
            current = [str(v) for v in conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()]
    except Exception:  # noqa: BLE001 - missing table / unreachable: same advice, never echo the driver message (may hold the URL)
        raise SystemExit(f"REFUSING to write: could not read alembic_version on this database. Bring the schema to head first: {UPGRADE_HINT}") from None
    if current != [head]:
        raise SystemExit(f"REFUSING to write: the schema is at {current or 'no revision'} but the code expects {head}. Run: {UPGRADE_HINT}")
    return head


# ---------------------------------------------------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    key: str
    role: str
    full_name: str
    email: str
    phone: int  # 639XXXXXXXXX, the integer form users.phone stores
    staff_id: int | None

    @property
    def local_phone(self) -> str:
        return "0" + str(self.phone)[2:]


def build_accounts(driver_staff_id: int = DEFAULT_DRIVER_STAFF_ID, helper_staff_id: int = DEFAULT_HELPER_STAFF_ID) -> list[Account]:
    return [
        Account("driver", "driver", "TRIPE2E Test Driver", "trip-test-driver@rgf.test", 639170090001, driver_staff_id),
        Account("helper", "helper", "TRIPE2E Test Helper", "trip-test-helper@rgf.test", 639170090002, helper_staff_id),
        Account("officer", "logistics_officer", "TRIPE2E Test Officer", "trip-test-officer@rgf.test", 639170090003, None),
    ]




@dataclass
class SeedResult:
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    invite_paths: dict[str, str] = field(default_factory=dict)
    generated_password: str | None = None


def seed_accounts(Session, *, apply: bool, password: str | None = None, force_change: bool = False, with_invite: bool = False,
                  driver_staff_id: int = DEFAULT_DRIVER_STAFF_ID, helper_staff_id: int = DEFAULT_HELPER_STAFF_ID, emit=print) -> SeedResult:
    """Create the three test users that do not exist yet. Idempotent. Writes only when apply=True."""
    from sqlalchemy import select

    from auth.security import hash_password, password_problem
    from models.user import User
    from services import user_invites

    result = SeedResult()
    if password:
        problem = password_problem(password)
        if problem:
            raise SystemExit(f"--password: {problem}")
    accounts = build_accounts(driver_staff_id, helper_staff_id)
    if driver_staff_id == helper_staff_id:
        raise SystemExit("--driver-staff-id and --helper-staff-id must differ.")
    with Session() as db:
        to_create: list[Account] = []
        existing_users: dict[str, User] = {}
        for account in accounts:
            found = db.execute(select(User).where(User.email == account.email)).scalar_one_or_none()
            if found is not None:
                existing_users[account.key] = found
                note = "" if (found.role == account.role and found.staff_id == account.staff_id) else f" BUT role/staff differ (role={found.role}, staff_id={found.staff_id})"
                emit(f"{account.key:8} {account.email}: already exists (user id {found.id}) - left unchanged{note}")
                result.existing.append(account.key)
                if note:
                    result.problems.append(f"{account.key}: existing user does not match the expected role / staff link")
                continue
            clash_filter = (User.phone == account.phone) if account.staff_id is None else ((User.phone == account.phone) | (User.staff_id == account.staff_id))
            clash = db.execute(select(User).where(clash_filter)).scalars().first()
            if clash is not None:
                why = "phone" if clash.phone == account.phone else f"staff id {account.staff_id}"
                emit(f"{account.key:8} {account.email}: SKIPPED - {why} is already used by user id {clash.id}")
                result.problems.append(f"{account.key}: {why} already used by user id {clash.id}")
                continue
            to_create.append(account)
            emit(f"{account.key:8} {account.email}: {'creating' if apply else 'would create'} role={account.role} staff_id={account.staff_id} phone={account.local_phone}")
        if apply and to_create:
            plain = password or secrets.token_urlsafe(12)
            result.generated_password = None if password else plain
            for account in to_create:
                user = User(full_name=account.full_name, email=account.email, phone=account.phone, role=account.role, status="active", staff_id=account.staff_id,
                            password_hash=hash_password(plain), must_change_password=bool(force_change))
                db.add(user)
                existing_users[account.key] = user
                result.created.append(account.key)
            db.flush()
        elif not apply:
            result.created.extend(a.key for a in to_create)
        if with_invite and not apply:
            emit("would create a set-password invite link for each test user (needs --apply)")
        elif with_invite:
            for account in accounts:
                user = existing_users.get(account.key)
                if user is not None:
                    token, _expires = user_invites.create_invite(db, user, "invite")
                    result.invite_paths[account.key] = f"/set-password?token={token}"
        if apply:
            db.commit()
    if result.generated_password:
        emit(f"\nGenerated password for the new test users (shown once, not stored anywhere): {result.generated_password}")
    for key, path in result.invite_paths.items():
        emit(f"invite {key}: {path}")
    emit(f"\n{'Created' if apply else 'Would create'} {len(result.created)}, already existed {len(result.existing)}, problems {len(result.problems)}." + ("" if apply else "  (dry run - add --apply to write)"))
    return result


# ---------------------------------------------------------------------------------------------------------------------
# End-to-end flow (in-process through TestClient; everything external is blocked)
# ---------------------------------------------------------------------------------------------------------------------


class _Abort(Exception):
    pass


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        return bool(ok)

    def require(self, name: str, ok: bool, detail: str = "") -> None:
        if not self.check(name, ok, detail):
            raise _Abort(name)

    @property
    def ok(self) -> bool:
        return bool(self.rows) and all(ok for _, ok, _ in self.rows)

    def table(self) -> str:
        width = max([len(name) for name, _, _ in self.rows] + [4])
        lines = [f"{'RESULT':6}  {'STEP':{width}}  DETAIL"]
        lines += [f"{'PASS' if ok else 'FAIL':6}  {name:{width}}  {detail}" for name, ok, detail in self.rows]
        failed = sum(1 for _, ok, _ in self.rows if not ok)
        lines.append(f"\n{len(self.rows) - failed} passed, {failed} failed.")
        return "\n".join(lines)


@contextmanager
def _patched(obj, name, value):
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)


def build_ack_answers(template: dict) -> list[dict]:
    """Answer an acknowledge checklist: tick every checkbox, count 1, fill required text. Photo items are never answered
    (photo storage is forced off for the run)."""
    answers = []
    for item in template["items"]:
        kind = item["item_type"]
        if kind == "checkbox":
            answers.append({"item_id": item["id"], "checked": True})
        elif kind == "count":
            answers.append({"item_id": item["id"], "count": 1})
        elif kind in ("note", "text") and item.get("required"):
            answers.append({"item_id": item["id"], "text": "TRIPE2E test"})
    return answers


def build_checkout_answers(template: dict, outcome: str = "Delivered") -> dict:
    """Valid delivery_checkout answers from the ACTIVE template: the outcome, every visible required item that the server
    does not prefill, nothing for photos / signatures."""
    options = next((i.get("options") or [] for i in template["items"] if i["key"] == "outcome"), [])
    answers: dict = {"outcome": outcome if outcome in options or not options else options[0]}
    for item in template["items"]:
        if item["key"] == "outcome" or item.get("prefill") or item["item_type"] in ("photo", "signature"):
            continue
        rule = item.get("show_when")
        if isinstance(rule, dict) and rule.get("key") and answers.get(rule["key"]) not in (rule.get("in") or []):
            continue
        if not item.get("required"):
            continue
        kind = item["item_type"]
        answers[item["key"]] = (
            True if kind == "checkbox" else 1 if kind == "number" else (item.get("options") or ["TRIPE2E"])[0] if kind in ("dropdown", "radio")
            else datetime.now(timezone.utc).date().isoformat() if kind == "date" else datetime.now(timezone.utc).isoformat() if kind == "datetime" else "TRIPE2E test"
        )
    return answers


def _action_body(**extra) -> dict:
    return {"idempotency_key": str(uuid.uuid4()), "client_ts": datetime.now(timezone.utc).isoformat(), "gps": {"lat": 14.55, "lng": 121.02, "accuracy_m": 12.5}, "gps_denied_reason": None, **extra}


def _brief(response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    detail = body.get("detail") if isinstance(body, dict) else body
    return f"HTTP {response.status_code} {str(detail)[:160]}"


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%y%m%d%H%M%S") + uuid.uuid4().hex[:4]


def cleanup_batch(Session, batch_id: str, *, delete_users: bool = False, driver_staff_id: int = DEFAULT_DRIVER_STAFF_ID, helper_staff_id: int = DEFAULT_HELPER_STAFF_ID) -> dict[str, int]:
    """Delete ONLY the rows of one TRIPE2E- run, each statement scoped by that batch id (or by the exact ids / test emails).
    Never a table-wide DELETE. Returns rows removed per table."""
    from sqlalchemy import delete, select

    from models.sales_order_history import SalesOrderHistory
    from models.sales_order_lines import SalesOrderLine
    from models.trip import InvoiceHandover, InvoiceHandoverItem, SoAcknowledgement, Trip, TripEvent, TripPhoto, TripStop
    from models.user import User, UserInvite

    if not str(batch_id).startswith(BATCH_PREFIX) or len(batch_id) <= len(BATCH_PREFIX):
        raise SystemExit(f"Refusing to clean up: batch id must start with {BATCH_PREFIX}")
    removed: dict[str, int] = {}
    with Session() as db:
        def run(label, statement):
            removed[label] = db.execute(statement).rowcount or 0

        trip_ids = list(db.execute(select(Trip.id).where(Trip.batch_id == batch_id)).scalars().all())
        handover_ids = list(db.execute(select(InvoiceHandover.id).where(InvoiceHandover.batch_id == batch_id)).scalars().all())
        so_ids = [i for i in db.execute(select(SalesOrderHistory.id).where(SalesOrderHistory.assignment_batch_id == batch_id)).scalars().all() if str(i).startswith(FAKE_PREFIX)]
        if handover_ids:
            run("invoice_handover_items", delete(InvoiceHandoverItem).where(InvoiceHandoverItem.handover_id.in_(handover_ids)))
        run("invoice_handovers", delete(InvoiceHandover).where(InvoiceHandover.batch_id == batch_id))
        run("so_acknowledgements", delete(SoAcknowledgement).where(SoAcknowledgement.batch_id == batch_id))
        if trip_ids:
            run("trip_stops", delete(TripStop).where(TripStop.trip_id.in_(trip_ids)))
        run("trip_events", delete(TripEvent).where(TripEvent.batch_id == batch_id))
        run("trip_photos", delete(TripPhoto).where(TripPhoto.batch_id == batch_id))
        run("trips", delete(Trip).where(Trip.batch_id == batch_id))
        if so_ids:
            run("sales_order_lines", delete(SalesOrderLine).where(SalesOrderLine.sales_order_id.in_(so_ids)))
            run("sales_orders", delete(SalesOrderHistory).where(SalesOrderHistory.id.in_(so_ids), SalesOrderHistory.assignment_batch_id == batch_id))
        if delete_users:
            emails = [a.email for a in build_accounts(driver_staff_id, helper_staff_id)]
            user_ids = list(db.execute(select(User.id).where(User.email.in_(emails))).scalars().all())
            if user_ids:
                run("user_invites", delete(UserInvite).where(UserInvite.user_id.in_(user_ids)))
                run("users", delete(User).where(User.id.in_(user_ids)))
        db.commit()
    return removed


def count_batch_rows(Session, batch_id: str) -> int:
    """Rows still carrying this batch id (used to prove cleanup removed everything)."""
    from sqlalchemy import func, select

    from models.sales_order_history import SalesOrderHistory
    from models.trip import InvoiceHandover, SoAcknowledgement, Trip, TripEvent

    with Session() as db:
        total = sum(db.scalar(select(func.count()).select_from(m).where(col == batch_id)) or 0
                    for m, col in ((Trip, Trip.batch_id), (SoAcknowledgement, SoAcknowledgement.batch_id), (TripEvent, TripEvent.batch_id),
                                   (InvoiceHandover, InvoiceHandover.batch_id), (SalesOrderHistory, SalesOrderHistory.assignment_batch_id)))
    return int(total)


def run_trip_e2e(Session, *, password: str, n_orders: int = 3, cleanup: bool = False, cleanup_users: bool = False, emit=print,
                 driver_staff_id: int = DEFAULT_DRIVER_STAFF_ID, helper_staff_id: int = DEFAULT_HELPER_STAFF_ID) -> Report:
    """Run the whole trip flow in-process. `Session` is a session factory for the target database. Fake SOs are written as
    ordinary sales_orders rows (the driver endpoints read assigned SOs from there) AND injected into the in-memory live cache;
    the staff directory cache is replaced in memory. Zoho / n8n / photo storage are blocked for the duration."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    import database
    from auth import dependencies
    from models.inventory import SalesOrderCache
    from models.sales_order_history import SalesOrderHistory
    from models.trip import InvoiceHandover, Trip, TripEvent
    from models.user import User
    from routers import auth as auth_router, driver as driver_router, driver_trip, officer
    from services import driver_assignments, live_sales_order_cache, media_storage, staff_directory_cache, user_cache, zoho_client

    report = Report()
    accounts = {a.key: a for a in build_accounts(driver_staff_id, helper_staff_id)}
    run = new_run_id()
    batch_id = f"{BATCH_PREFIX}{run}"
    so_ids = [f"{FAKE_PREFIX}SO-{run}-{i:02d}" for i in range(1, n_orders + 1)]
    external_calls: list[str] = []

    def blocked(name):
        def refuse(*args, **kwargs):
            external_calls.append(name)
            raise AssertionError(f"e2e tried to call {name}: external calls are blocked")
        return refuse

    app = FastAPI()
    for router in (auth_router.router, driver_router.router, driver_trip.router, officer.router):
        app.include_router(router)

    def get_run_db():
        with Session() as db:
            yield db

    app.dependency_overrides[database.get_db] = get_run_db
    client = TestClient(app, raise_server_exceptions=False)  # no lifespan: no scheduler, no Cartrack poller, no startup warm-ups
    today = driver_assignments.manila_today()
    original_staff = list(staff_directory_cache._staff)

    try:
        with ExitStack() as stack:
            stack.enter_context(_patched(dependencies, "SessionLocal", Session))
            stack.enter_context(_patched(zoho_client, "_request_http", blocked("Zoho")))
            stack.enter_context(_patched(live_sales_order_cache, "fetch_sales_order_detail", blocked("Zoho SO detail")))
            stack.enter_context(_patched(live_sales_order_cache, "ensure_zoho_data", blocked("Zoho SO fetch")))
            stack.enter_context(_patched(media_storage, "is_enabled", lambda: False))
            stack.enter_context(_patched(staff_directory_cache, "_refresh_if_stale", lambda: None))
            stack.enter_context(_patched(staff_directory_cache, "_rest_rows", blocked("n8n REST")))
            stack.enter_context(_patched(staff_directory_cache, "_webhook_staff", blocked("n8n webhook")))
            stack.enter_context(_patched(staff_directory_cache, "refresh", blocked("n8n refresh")))
            user_cache._cache.clear()
            driver_assignments.reset_cache()

            try:
                # ---- accounts -----------------------------------------------------------------------------------------
                with Session() as db:
                    users = {key: db.execute(select(User).where(User.email == acc.email)).scalar_one_or_none() for key, acc in accounts.items()}
                missing = [key for key, user in users.items() if user is None]
                report.require("test users exist (run `seed --apply` first)", not missing, f"missing: {', '.join(missing)}" if missing else "driver, helper, officer found")
                wrong = [k for k, u in users.items() if u.role != accounts[k].role or u.status not in (None, "active") or (accounts[k].staff_id is not None and u.staff_id != accounts[k].staff_id)]
                report.require("test users have the expected role / staff link / active status", not wrong, f"mismatch: {', '.join(wrong)}" if wrong else "ok")
                driver_staff, helper_staff = users["driver"].staff_id, users["helper"].staff_id
                officer_id = users["officer"].id

                # ---- fake data: sales_orders rows (what the driver app reads) + in-memory caches -----------------------
                fakes = [
                    {"id": driver_staff, "name": "TRIPE2E Test Driver", "title": "MC RIDER", "warehouse": "METS", "email": accounts["driver"].email, "phone": accounts["driver"].local_phone, "role": "DRIVER", "active": True},
                    {"id": helper_staff, "name": "TRIPE2E Test Helper", "title": "DELIVERY HELPER", "warehouse": "METS", "email": accounts["helper"].email, "phone": accounts["helper"].local_phone, "role": "HELPER", "active": True},
                ]
                with staff_directory_cache._lock:
                    staff_directory_cache._staff[:] = [s for s in original_staff if s.get("id") not in (driver_staff, helper_staff)] + fakes
                now = datetime.now(timezone.utc)
                with Session() as db:
                    for index, so_id in enumerate(so_ids):
                        db.add(SalesOrderHistory(
                            id=so_id, salesorder_number=so_id, customer_name=f"TRIPE2E Customer {index + 1}", expected_shipment_date=today, synced_at=now,
                            shipping_address={"address": f"TRIPE2E Test Street {index + 1}", "city": "Makati"}, vehicle_id="TRIPE2E-TRUCK", driver_id=driver_staff,
                            helper_ids=[driver_staff, helper_staff], assigned_at=now + timedelta(seconds=index), assignment_status="assigned", assignment_batch_id=batch_id,
                            delivery_contact={"person": "TRIPE2E Contact", "phone": None, "attention": None}))
                    db.commit()
                for index, so_id in enumerate(so_ids):
                    live_sales_order_cache.set_assignment(so_id, vehicle_id="TRIPE2E-TRUCK", driver_id=driver_staff, assignment_status="assigned", assigned_at=now + timedelta(seconds=index),
                                                          helper_ids=[driver_staff, helper_staff], assignment_batch_id=batch_id)
                    live_sales_order_cache._assigned_zoho[so_id] = SalesOrderCache(
                        id=so_id, salesorder_number=so_id, customer_name=f"TRIPE2E Customer {index + 1}", expected_shipment_date=today, synced_at=now, shipping_address={"city": "Makati"},
                        raw_json={"salesorder_id": so_id, "line_items": [{"name": "TRIPE2E item"}]}, assignment_status="assigned", driver_id=driver_staff, vehicle_id="TRIPE2E-TRUCK")
                report.require(f"inject {n_orders} fake SOs for batch {batch_id}", True, f"ids {so_ids[0]} .. {so_ids[-1]}; in-memory caches + staff directory faked")

                def post(path, token, **payload):
                    return client.post(path, headers={"Authorization": f"Bearer {token}"}, json=payload)

                def get(path, token):
                    return client.get(path, headers={"Authorization": f"Bearer {token}"})

                def login(identifier):
                    return client.post("/auth/login", json={"identifier": identifier, "password": password})

                def batch_url(suffix):
                    return f"/api/driver/batches/{batch_id}/{suffix}"

                def event_count():
                    with Session() as db:
                        return db.scalar(select(func.count()).select_from(TripEvent).where(TripEvent.batch_id == batch_id)) or 0

                # ---- logins ------------------------------------------------------------------------------------------
                r = login(accounts["driver"].local_phone)
                report.require("driver logs in with phone number", r.status_code == 200 and r.json().get("user", {}).get("role") == "driver", _brief(r) if r.status_code != 200 else "role driver")
                dt = r.json()["token"]
                r = login(accounts["helper"].email)
                report.require("helper logs in with email", r.status_code == 200 and r.json().get("user", {}).get("role") == "helper", _brief(r) if r.status_code != 200 else "role helper")
                ht = r.json()["token"]

                # ---- driver today + templates ------------------------------------------------------------------------
                r = get("/api/driver/today", dt)
                mine = next((b for b in (r.json().get("batches", []) if r.status_code == 200 else []) if b["batch_id"] == batch_id), None)
                report.require("GET /api/driver/today lists the fake batch", mine is not None and mine["so_count"] == n_orders and mine["trip"]["chip"] == "to_acknowledge",
                               _brief(r) if mine is None else f"{mine['so_count']} SOs, chip {mine['trip']['chip']}, crew {[c['role'] for c in mine['crew']]}")
                report.check("batch crew = driver + helper", sorted(c["role"] for c in mine["crew"]) == ["driver", "helper"])
                r = get("/api/driver/templates", dt)
                report.require("GET /api/driver/templates (active ack + checkout templates)", r.status_code == 200 and r.json().get("acknowledge_checklist") and r.json().get("delivery_checkout"), _brief(r))
                templates = r.json()
                r = get("/api/driver/officers", dt)
                report.require("test officer listed in GET /api/driver/officers", r.status_code == 200 and any(o["id"] == officer_id for o in r.json()), _brief(r))

                # ---- acknowledge every SO ----------------------------------------------------------------------------
                ack_body = None
                for so_id in so_ids:
                    body = _action_body(template_id=templates["acknowledge_checklist"]["id"], answers=build_ack_answers(templates["acknowledge_checklist"]))
                    ack_body = ack_body or (so_id, body)
                    r = post(batch_url(f"orders/{so_id}/acknowledge"), dt, **body)
                    report.require(f"acknowledge {so_id[-6:]}", r.status_code == 200 and r.json().get("ok") and not r.json().get("already_done"), _brief(r))
                events_before = event_count()
                r = post(batch_url(f"orders/{ack_body[0]}/acknowledge"), dt, **ack_body[1])
                report.check("replaying an acknowledge with the same idempotency_key returns replayed=true", r.status_code == 200 and r.json().get("replayed") is True, _brief(r))
                report.check("the replay wrote nothing (event count unchanged)", event_count() == events_before)
                r = post(batch_url(f"orders/{so_ids[0]}/acknowledge"), ht, **_action_body(template_id=templates["acknowledge_checklist"]["id"], answers=build_ack_answers(templates["acknowledge_checklist"])))
                body = r.json() if r.status_code == 200 else {}
                report.check("helper repeating the driver's acknowledge -> already_done", body.get("already_done") is True and (body.get("done_by") or {}).get("role") == "driver", _brief(r))

                # ---- start -------------------------------------------------------------------------------------------
                r = post(batch_url("start"), dt, **_action_body())
                report.require("driver starts the trip", r.status_code == 200 and r.json()["batch"]["trip"]["chip"] == "on_trip", _brief(r))
                r = post(batch_url("start"), ht, **_action_body())
                body = r.json() if r.status_code == 200 else {}
                report.check("helper repeating start -> already_done (not 409)", body.get("already_done") is True and (body.get("done_by") or {}).get("role") == "driver", _brief(r))

                # ---- every stop: check-in then check-out -------------------------------------------------------------
                r = get(batch_url("route"), dt)
                stops = [s["so_id"] for s in r.json()["stops"]] if r.status_code == 200 else []
                report.require("route lists every SO as a stop", sorted(stops) == sorted(so_ids), _brief(r) if r.status_code != 200 else f"{len(stops)} stops")
                checkout_template = templates["delivery_checkout"]
                for number, so_id in enumerate(stops, 1):
                    r = post(batch_url(f"orders/{so_id}/check-in"), dt, **_action_body())
                    report.require(f"stop {number}/{len(stops)} check-in", r.status_code == 200 and not r.json().get("already_done"), _brief(r))
                    if number == 1:
                        r = post(batch_url(f"orders/{so_id}/check-in"), ht, **_action_body())
                        report.check("helper repeating check-in -> already_done", r.status_code == 200 and r.json().get("already_done") is True, _brief(r))
                    r = post(batch_url(f"orders/{so_id}/check-out"), dt, **_action_body(template_id=checkout_template["id"], answers=build_checkout_answers(checkout_template)))
                    report.require(f"stop {number}/{len(stops)} check-out (Delivered)", r.status_code == 200 and not r.json().get("already_done"), _brief(r))

                # ---- end + handover ----------------------------------------------------------------------------------
                r = post(batch_url("end"), dt, **_action_body())
                report.require("driver ends the trip", r.status_code == 200 and r.json()["batch"]["trip"]["chip"] == "returned", _brief(r))
                items = [{"so_id": so_id, "handed_over": True} for so_id in so_ids]
                r = post(batch_url("handover"), dt, **_action_body(officer_user_id=officer_id, items=items))
                report.require("driver submits the invoice handover to the test officer", r.status_code == 200 and r.json()["batch"]["trip"]["chip"] == "handover_pending", _brief(r))

                # ---- officer validates -------------------------------------------------------------------------------
                r = login(accounts["officer"].email)
                report.require("officer logs in", r.status_code == 200 and r.json().get("user", {}).get("role") == "logistics_officer", _brief(r) if r.status_code != 200 else "role logistics_officer")
                ot = r.json()["token"]
                r = get("/api/officer/handovers?status=pending", ot)
                row = next((h for h in (r.json() if r.status_code == 200 else []) if h["batch_id"] == batch_id), None)
                report.require("officer queue shows the handover (for_me)", row is not None and row["for_me"] is True and row["totals"]["invoices"] == n_orders, _brief(r) if row is None else f"handover {row['handover_id']}, {row['totals']['invoices']} invoices")
                r = get(f"/api/officer/handovers/{row['handover_id']}", ot)
                report.require("officer opens the handover detail", r.status_code == 200 and len(r.json()["items"]) == n_orders, _brief(r))
                item_ids = [i["item_id"] for i in r.json()["items"]]
                validate = _action_body(items=[{"item_id": item_id, "status": "received"} for item_id in item_ids])
                r = post(f"/api/officer/handovers/{row['handover_id']}/validate", ot, **validate)
                report.require("officer validates every invoice as received -> handover closed", r.status_code == 200 and r.json()["handover"]["status"] == "closed", _brief(r))
                events_before = event_count()
                r = post(f"/api/officer/handovers/{row['handover_id']}/validate", ot, **validate)
                report.check("replaying the officer validation returns replayed=true", r.status_code == 200 and r.json().get("replayed") is True, _brief(r))
                report.check("the officer replay wrote nothing", event_count() == events_before)

                # ---- final state -------------------------------------------------------------------------------------
                with Session() as db:
                    trip = db.execute(select(Trip).where(Trip.batch_id == batch_id)).scalar_one_or_none()
                    handover = db.execute(select(InvoiceHandover).where(InvoiceHandover.batch_id == batch_id)).scalar_one_or_none()
                report.check("trip status is closed (database)", trip is not None and trip.status == "closed" and trip.closed_at is not None, f"status {getattr(trip, 'status', None)}")
                report.check("handover status is closed (database)", handover is not None and handover.status == "closed", f"status {getattr(handover, 'status', None)}")
                r = get(f"/api/driver/assignments/{batch_id}", dt)
                report.check("driver app shows chip 'closed'", r.status_code == 200 and r.json()["trip"]["chip"] == "closed" and r.json()["trip"]["status"] == "closed", _brief(r) if r.status_code != 200 else "chip closed")
                expected_events = 4 * n_orders + 5  # acks + start + (in+out) per stop + end + handover + (officer_acknowledged + invoice_validated per SO + trip_closed)
                report.check(f"exactly {expected_events} trip events (duplicates and replays wrote nothing)", event_count() == expected_events, f"found {event_count()}")
                report.check("no Zoho / n8n call was attempted", not external_calls, ", ".join(external_calls) if external_calls else "0 external calls")
            except _Abort:
                pass
            except Exception as exc:  # noqa: BLE001 - recorded as a failed step; message only (never the traceback / connection details)
                report.check("unexpected error", False, f"{type(exc).__name__}: {str(exc)[:200]}")
            finally:
                with live_sales_order_cache._state_lock:
                    for so_id in so_ids:
                        live_sales_order_cache._assignment_state.pop(so_id, None)
                        live_sales_order_cache._assigned_zoho.pop(so_id, None)
                live_sales_order_cache.bump_assignment_generation()
                with staff_directory_cache._lock:
                    staff_directory_cache._staff[:] = original_staff
                user_cache._cache.clear()
                driver_assignments.reset_cache()
    finally:
        if cleanup or cleanup_users:
            try:
                removed = cleanup_batch(Session, batch_id, delete_users=cleanup_users, driver_staff_id=driver_staff_id, helper_staff_id=helper_staff_id)
                left = count_batch_rows(Session, batch_id)
                report.check(f"cleanup removed this run's rows ({batch_id})", left == 0, ", ".join(f"{k}={v}" for k, v in removed.items() if v) + f"; rows left with this batch id: {left}")
            except Exception as exc:  # noqa: BLE001
                report.check("cleanup", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        else:
            emit(f"Fake rows were left in the database. Remove them with:  python scripts/trip_test_accounts.py cleanup --batch-id {batch_id} --branch-host <host> --confirm-neon-branch")
    return report


# ---------------------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed test logins and run the trip workflow end to end against a throwaway database.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--branch-host", help="host of the Neon branch in DATABASE_URL (typed on purpose; must match exactly)")
        p.add_argument("--confirm-neon-branch", action="store_true", help="required for any neon.tech host: I confirm this is a throwaway branch")

    seed = sub.add_parser("seed", help="create the driver / helper / officer test users (dry run unless --apply)")
    common(seed)
    seed.add_argument("--driver-staff-id", type=int, default=DEFAULT_DRIVER_STAFF_ID)
    seed.add_argument("--helper-staff-id", type=int, default=DEFAULT_HELPER_STAFF_ID)
    seed.add_argument("--password", help="password for the new users (default: generated and printed once)")
    seed.add_argument("--force-change", action="store_true", help="set must_change_password=true on the new users")
    seed.add_argument("--with-invite", action="store_true", help="also create a set-password invite link per user (path printed once)")
    seed.add_argument("--apply", action="store_true", help="actually write; without it nothing is changed")

    e2e = sub.add_parser("e2e", help="run the full trip flow in-process with fake TRIPE2E- sales orders")
    common(e2e)
    e2e.add_argument("--password", default=os.environ.get("TRIP_TEST_PASSWORD"), help="the test users' password (or env TRIP_TEST_PASSWORD)")
    e2e.add_argument("--orders", type=int, default=3, help="number of fake Sales Orders in the batch (default 3)")
    e2e.add_argument("--cleanup", action="store_true", help="afterwards delete only the rows this run created")
    e2e.add_argument("--cleanup-users", action="store_true", help="also delete the three test users (implies --cleanup)")
    e2e.add_argument("--driver-staff-id", type=int, default=DEFAULT_DRIVER_STAFF_ID)
    e2e.add_argument("--helper-staff-id", type=int, default=DEFAULT_HELPER_STAFF_ID)

    clean = sub.add_parser("cleanup", help="delete the rows of one earlier e2e run")
    common(clean)
    clean.add_argument("--batch-id", required=True, help=f"the {BATCH_PREFIX}... id printed by the e2e run")
    clean.add_argument("--cleanup-users", action="store_true", help="also delete the three test users")
    clean.add_argument("--driver-staff-id", type=int, default=DEFAULT_DRIVER_STAFF_ID)
    clean.add_argument("--helper-staff-id", type=int, default=DEFAULT_HELPER_STAFF_ID)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "e2e":
        if not 1 <= args.orders <= 20:
            raise SystemExit("--orders must be between 1 and 20.")
        if not args.password:
            raise SystemExit("e2e needs the test users' password: --password or env TRIP_TEST_PASSWORD.")

    # 1. Guard FIRST: nothing below is imported (database.py would call load_dotenv and build an engine) until it passes.
    info = assert_safe_database(os.environ.get("DATABASE_URL", ""), branch_host=args.branch_host, environ=os.environ, prod_url_candidates=read_env_file_urls(ENV_PATH),
                                confirm_neon_branch=args.confirm_neon_branch)
    print(f"Target database host: {info['host'] or 'sqlite'}")
    if not os.environ.get("DATABASE_URL_DIRECT"):
        os.environ["DATABASE_URL_DIRECT"] = os.environ["DATABASE_URL"]  # so load_dotenv cannot bring backend/.env's production value in

    # 2. Only now load the app (load_dotenv() does not override the variables exported above).
    sys.path.insert(0, BACKEND_DIR)
    import database

    if info["host"] and (database.engine.url.host or "").lower() != info["host"]:
        raise SystemExit("REFUSING to run: the engine host differs from the guarded DATABASE_URL host.")
    assert_schema_at_head(database.engine)
    Session = database.SessionLocal

    if args.command == "seed":
        result = seed_accounts(Session, apply=args.apply, password=args.password, force_change=args.force_change, with_invite=args.with_invite,
                               driver_staff_id=args.driver_staff_id, helper_staff_id=args.helper_staff_id)
        return 1 if result.problems else 0
    if args.command == "cleanup":
        removed = cleanup_batch(Session, args.batch_id, delete_users=args.cleanup_users, driver_staff_id=args.driver_staff_id, helper_staff_id=args.helper_staff_id)
        print("Removed: " + (", ".join(f"{k}={v}" for k, v in removed.items() if v) or "nothing"))
        return 0
    report = run_trip_e2e(Session, password=args.password, n_orders=args.orders, cleanup=args.cleanup or args.cleanup_users, cleanup_users=args.cleanup_users,
                          driver_staff_id=args.driver_staff_id, helper_staff_id=args.helper_staff_id)
    print(report.table())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
