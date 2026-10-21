"""Load Planning must never hold a request through a long cold Zoho fetch, and every failure must reach the browser WITH CORS headers.
Mocked only: the Zoho pull is a fake that blocks on an Event; any real Zoho call fails the test."""
from __future__ import annotations

import os
import threading
import time
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test-secret-not-real")

from services import live_sales_order_cache as cache, safe_errors, zoho_error_handler  # noqa: E402
from services.zoho_client import ZohoError  # noqa: E402

ORIGIN = "https://app.example.test"


@pytest.fixture()
def warm(monkeypatch):
    release = threading.Event()
    calls = {"pull": 0}

    def fake_pull(start, end):
        calls["pull"] += 1
        release.wait(5)
        return {}

    monkeypatch.setattr(cache, "_pull_window", fake_pull)
    monkeypatch.setattr(cache, "_hydrate_details", lambda raw: None)
    monkeypatch.setattr(cache, "fetch_sales_order_detail", lambda *a, **k: (_ for _ in ()).throw(AssertionError("direct Zoho call")))
    saved = dict(cache._windows)
    cache._windows.clear()
    cache._warm_failed.clear()
    cache._warming.clear()
    yield release, calls
    release.set()
    for thread in list(cache._warming.values()):
        thread.join(5)
    cache._windows.clear()
    cache._windows.update(saved)
    cache._warm_failed.clear()


TODAY = date(2026, 10, 13)


def test_a_cold_window_answers_immediately_and_fills_in_the_background_once(warm):
    release, calls = warm
    started = time.monotonic()
    for _ in range(3):
        with pytest.raises(cache.WindowWarming):
            cache.get_window(TODAY, TODAY, block=False)
    assert time.monotonic() - started < 1.0  # nobody waited for the (blocked) Zoho pull
    time.sleep(0.2)
    assert calls["pull"] == 1  # single flight: three requests, one pull
    release.set()
    for thread in list(cache._warming.values()):
        thread.join(5)
    assert cache.get_window(TODAY, TODAY, block=False) == []  # the cache is warm now: served from memory, no pull
    assert calls["pull"] == 1


def test_the_blocking_call_is_unchanged_for_other_callers(warm):
    release, calls = warm
    release.set()
    assert cache.get_window(TODAY, TODAY) == []
    assert calls["pull"] == 1


def test_a_failed_background_fill_is_reported_to_the_next_request_not_retried_per_request(warm, monkeypatch):
    release, calls = warm

    def boom(start, end):
        calls["pull"] += 1
        raise ZohoError("Zoho daily budget guard active; non-essential Zoho calls are paused.")

    monkeypatch.setattr(cache, "_pull_window", boom)
    with pytest.raises(cache.WindowWarming):
        cache.get_window(TODAY, TODAY, block=False)
    for thread in list(cache._warming.values()):
        thread.join(5)
    for _ in range(3):
        with pytest.raises(ZohoError, match="budget guard"):
            cache.get_window(TODAY, TODAY, block=False)
    assert calls["pull"] == 1  # the failure is remembered for a minute instead of hammering Zoho


def test_assigned_snapshot_non_blocking_warms_in_the_background(monkeypatch):
    fetched: list[str] = []

    def slow_ensure(order_id):
        fetched.append(order_id)
        row = type("R", (), {"id": order_id})()
        cache._assigned_zoho[order_id] = row
        return row

    monkeypatch.setattr(cache, "ensure_zoho_data", slow_ensure)
    saved_state, saved_zoho = dict(cache._assignment_state), dict(cache._assigned_zoho)
    cache._assignment_state.clear()
    cache._assigned_zoho.clear()
    cache._warm_failed.clear()
    from datetime import datetime

    cache._assignment_state["a1"] = {"assignment_status": "assigned", "expected_shipment_date": datetime.now(cache.PHT).date()}
    try:
        with pytest.raises(cache.WindowWarming):
            cache.get_assigned_snapshot_ex(include_window=False, block=False)
        for thread in list(cache._warming.values()):
            thread.join(5)
        rows, failures = cache.get_assigned_snapshot_ex(include_window=False, block=False)
        assert [r.id for r in rows] == ["a1"] and failures is False and fetched == ["a1"]
    finally:
        cache._assignment_state.clear()
        cache._assignment_state.update(saved_state)
        cache._assigned_zoho.clear()
        cache._assigned_zoho.update(saved_zoho)


def test_load_planning_list_endpoints_ask_for_the_non_blocking_read(monkeypatch):
    from routers import load_planning

    seen: list[dict] = []

    def fake_window(start, end, **kwargs):
        seen.append(kwargs)
        raise cache.WindowWarming("loading")

    monkeypatch.setattr(cache, "get_window", fake_window)
    monkeypatch.setattr(cache, "get_assigned_snapshot_ex", lambda *a, **k: (seen.append(k), (_ for _ in ()).throw(cache.WindowWarming("loading")))[1])
    for assignment in (None, "assigned"):
        with pytest.raises(cache.WindowWarming):
            load_planning._rows_any_branch(None, None, None, None, None, assignment)
    with pytest.raises(cache.WindowWarming):
        load_planning.list_sales_order_cities(date_from=None, date_to=None)
    assert seen and all(kwargs.get("block") is False for kwargs in seen)
    assert seen[1].get("include_window") is False


def test_confirmed_orders_use_warm_assigned_rows_without_loading_inventory(monkeypatch):
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from routers import load_planning

    day = datetime.now(cache.PHT).date() + timedelta(days=1)
    row = SimpleNamespace(id="cached-so", expected_shipment_date=day,
                          salesorder_number="SO26-100", order_type="SO", raw_json={},
                          assignment_status="assigned", customer_name="Customer",
                          reference_number=None, vehicle_id="truck", order_status="confirmed")
    monkeypatch.setattr(cache, "_assignment_state", {row.id: {
        "assignment_status": "assigned", "expected_shipment_date": day}})
    monkeypatch.setattr(cache, "_assigned_zoho", {row.id: row})
    monkeypatch.setattr(cache, "_excluded_ids", {})

    def unrelated_inventory(*args, **kwargs):
        raise AssertionError("Confirmed Orders must not fetch the Inventory window")

    monkeypatch.setattr(cache, "get_window", unrelated_inventory)
    monkeypatch.setattr(cache, "fetch_sales_order_detail", unrelated_inventory)
    rows = load_planning._rows_any_branch(None, day.isoformat(), day.isoformat(),
                                         "All", None, "assigned", order_types="SO,WM,PO,TO")
    assert [r.id for r in rows] == [row.id]


# ---- CORS on every failure ---------------------------------------------------------------------------------------------------


def make_app() -> FastAPI:
    """Same composition as main.py: catch-all BEFORE CORSMiddleware, handlers registered on the app."""
    app = FastAPI()
    safe_errors.install_catch_all(app)
    safe_errors.register(app)
    zoho_error_handler.register(app)
    app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.get("/warming")
    def warming():
        raise cache.WindowWarming("Sales orders are loading from Zoho. Try again in a few seconds.")

    @app.get("/zoho")
    def zoho():
        raise ZohoError("Zoho daily budget guard active; non-essential Zoho calls are paused.")

    @app.get("/boom")
    def boom():
        raise RuntimeError("something unexpected with a secret-looking detail")

    return app


@pytest.mark.parametrize("path,status,code", [("/warming", 503, "warming"), ("/zoho", 503, "zoho_unavailable"), ("/boom", 500, "internal_error")])
def test_every_failure_keeps_its_cors_headers_and_a_readable_body(path, status, code):
    client = TestClient(make_app(), raise_server_exceptions=False)
    response = client.get(path, headers={"Origin": ORIGIN})
    assert response.status_code == status and response.json()["code"] == code
    assert response.headers["access-control-allow-origin"] == ORIGIN
    if path == "/warming":
        assert response.headers["retry-after"] == "5"
    if path == "/boom":
        assert "secret" not in response.text  # the exception text is logged, not sent to the browser


def test_without_the_catch_all_an_uncaught_error_has_no_cors_headers():
    """The failure mode the catch-all removes: Starlette answers from outside CORS, so the browser sees only a network / CORS error."""
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN], allow_methods=["*"], allow_headers=["*"])

    @app.get("/boom")
    def boom():
        raise RuntimeError("x")

    response = TestClient(app, raise_server_exceptions=False).get("/boom", headers={"Origin": ORIGIN})
    assert response.status_code == 500 and "access-control-allow-origin" not in response.headers


def test_main_registers_the_catch_all_before_the_cors_middleware():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert source.index("safe_errors.install_catch_all(app)") < source.index("app.add_middleware(\n    CORSMiddleware")


def test_the_kill_switch_restores_the_blocking_read(warm, monkeypatch):
    release, calls = warm
    release.set()
    monkeypatch.setenv("LOAD_PLANNING_BLOCKING_READS", "true")
    assert cache.get_window(TODAY, TODAY, block=False) == []  # waited for the (instant) fill instead of raising
    assert calls["pull"] == 1
