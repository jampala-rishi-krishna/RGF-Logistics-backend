"""Test-suite-wide safety: the tests must NEVER touch the real (Neon) database, for anything.

Three layers, all active for the whole session:
1. DATABASE_URL / DATABASE_URL_DIRECT are replaced by an address nothing listens on BEFORE any app module is
   imported (python-dotenv never overrides an existing variable, so backend/.env cannot bring the real URL back).
2. Any attempt to resolve a *.neon.tech host raises.
3. Any attempt to open a connection through the app's engine is refused AND recorded; the test that tried fails
   (even if the code under test swallowed the exception).
"""
import os
import socket

import pytest

BLOCKED_DATABASE_URL = "postgresql://blocked:blocked@127.0.0.1:9/never_a_real_database"
for _name in ("DATABASE_URL", "DATABASE_URL_DIRECT"):
    os.environ[_name] = BLOCKED_DATABASE_URL

_real_getaddrinfo = socket.getaddrinfo


def _guarded_getaddrinfo(host, *args, **kwargs):
    text = host.decode("utf-8", "ignore") if isinstance(host, bytes) else str(host or "")
    if "neon.tech" in text.lower():
        raise RuntimeError(f"tests must never connect to the real database ({text})")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _guarded_getaddrinfo

import database  # noqa: E402  (must come after the environment override above)
from sqlalchemy import event  # noqa: E402

CONNECTION_ATTEMPTS: list[str] = []


@event.listens_for(database.engine, "do_connect")
def _refuse_connection(dialect, conn_rec, cargs, cparams):
    CONNECTION_ATTEMPTS.append(os.environ.get("PYTEST_CURRENT_TEST", "<outside a test>"))
    raise RuntimeError("tests must never open a database connection; pass fakes/sessions explicitly")


def pytest_sessionstart(session):
    leaked = [name for name in ("DATABASE_URL", "DATABASE_URL_DIRECT") if "neon.tech" in os.environ.get(name, "").lower()]
    if leaked or "neon.tech" in str(database.engine.url).lower():
        pytest.exit(f"refusing to run: a real database URL is configured ({leaked or 'engine'})", returncode=3)


@pytest.fixture(autouse=True)
def _no_real_database(monkeypatch):
    from services import assignment_email_status, vehicle_flags

    def blocked():
        raise RuntimeError("tests must pass db= explicitly; the real database is never opened from the test suite")

    monkeypatch.setattr(vehicle_flags, "_open_session", blocked)
    monkeypatch.setattr(assignment_email_status, "_open_session", blocked)  # status writes stay in memory under test
    before = len(CONNECTION_ATTEMPTS)
    yield
    attempts = CONNECTION_ATTEMPTS[before:]
    if attempts:
        pytest.fail(f"this test tried to open a database connection ({len(attempts)}x); the suite never touches a real database", pytrace=False)
