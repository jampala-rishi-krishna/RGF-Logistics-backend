"""Test-suite-wide safety: nothing in the tests may open a real database session through the Fleet Health helpers."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_database_for_issue_reports(monkeypatch):
    from services import vehicle_flags

    def blocked():
        raise RuntimeError("tests must pass db= explicitly; the real database is never opened from the test suite")

    monkeypatch.setattr(vehicle_flags, "_open_session", blocked)
    yield
