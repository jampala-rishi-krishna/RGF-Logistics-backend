"""The suite must never reach the real (Neon) database. These tests prove the guards in conftest.py are live."""
import os
import socket

import pytest
from sqlalchemy import text

import database
from tests import conftest as guard


def test_database_urls_point_at_a_dead_local_address_not_neon():
    for name in ("DATABASE_URL", "DATABASE_URL_DIRECT"):
        assert "neon.tech" not in os.environ[name].lower()
        assert "127.0.0.1:9/" in os.environ[name]
    assert "neon.tech" not in str(database.engine.url).lower()


def test_neon_hostnames_cannot_even_be_resolved():
    with pytest.raises(RuntimeError, match="never connect"):
        socket.getaddrinfo("ep-cool-name-123456.ap-southeast-1.aws.neon.tech", 5432)


def test_a_connection_attempt_through_the_app_engine_is_refused_and_recorded():
    with pytest.raises(Exception):
        with database.SessionLocal() as db:
            db.execute(text("select 1"))
    assert guard.CONNECTION_ATTEMPTS and "test_a_connection_attempt" in guard.CONNECTION_ATTEMPTS[-1]
    guard.CONNECTION_ATTEMPTS.clear()  # provoked on purpose; any other test leaving an attempt behind is failed by the fixture
