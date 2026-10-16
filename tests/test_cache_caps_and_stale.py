"""Memory bounds of the in-process Zoho caches, and Load Planning answering from the last good data when Zoho is failing / slow /
budget-blocked. Mocked only: every Zoho call is a fake (no network, no database)."""
from __future__ import annotations

import gc
import os
import threading
import time
import tracemalloc
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("JWT_SECRET", "test-secret-not-real")

from fastapi.responses import JSONResponse  # noqa: E402

from routers import load_planning, pipeline  # noqa: E402
from services import live_sales_order_cache as lc, purchase_orders, transfer_orders, zoho_acquisition, zoho_resilience, zoho_usage  # noqa: E402
from services.zoho_client import ZohoError  # noqa: E402

DAY = "2026-10-20"
BUDGET_MESSAGE = "Zoho daily budget guard active; non-essential Zoho calls are paused."


def budget_blocked(*args, **kwargs):
    """Exactly what zoho_client does when the guard refuses: a ZohoError raised from ZohoBudgetGuard."""
    try:
        raise zoho_usage.ZohoBudgetGuard(BUDGET_MESSAGE)
    except zoho_usage.ZohoBudgetGuard as exc:
        raise ZohoError(str(exc)) from exc


def timed_out(*args, **kwargs):
    raise ZohoError("Zoho request timed out")


def body_of(response) -> tuple[int, dict]:
    if isinstance(response, JSONResponse):
        import json

        return response.status_code, json.loads(response.body)
    return 200, response


def so_record(rid: str, ship: date, *, lines: int = 10, city: str = "Pasig") -> dict:
    """A realistic-sized SO detail: ~10 line items, addresses, custom fields."""
    return {
        "salesorder_id": rid, "salesorder_number": f"SO-{rid}", "customer_name": f"Customer {rid}", "status": "confirmed",
        "current_sub_status": "confirmed", "date": ship.isoformat(), "shipment_date": ship.isoformat(), "total": 12345.5,
        "branch_id": "4489499000001322444", "branch_name": "Rare Global Food Trading Corp.", "last_modified_time": "2026-10-14T08:00:00+0800",
        "shipping_address": {"address": f"{rid} Some Street, Barangay Kapitolyo", "city": city, "state": "Metro Manila", "zip": "1603", "country": "Philippines", "phone": "+63 900 000 0000"},
        "billing_address": {"address": f"{rid} Billing Street", "city": city, "state": "Metro Manila", "zip": "1603", "country": "Philippines"},
        "custom_fields": [{"label": f"Field {n}", "value": f"value {n} for {rid}"} for n in range(6)],
        "line_items": [{"line_item_id": f"{rid}-{n}", "item_id": f"item-{n}", "name": f"Frozen product number {n}", "sku": f"SKU-{n:04d}", "quantity": 3 + n,
                        "unit": "kg", "rate": 450.25, "item_total": 1350.75, "warehouse_id": "4489499000064563124", "warehouse_name": "Mets Cold Storage - RGF",
                        "description": "Imported, keep frozen at -18C", "tax_name": "VAT", "tax_percentage": 12} for n in range(lines)],
    }


@pytest.fixture
def clean_lc():
    """Save and restore every live_sales_order_cache structure a test touches."""
    saved = (dict(lc._windows), dict(lc._windows_unhydrated), dict(lc._assigned_zoho), dict(lc._details), dict(lc._assignment_state), dict(lc._warm_failed))
    lc._windows.clear(); lc._windows_unhydrated.clear(); lc._assigned_zoho.clear(); lc._details.clear(); lc._warm_failed.clear()
    yield
    for thread in list(lc._warming.values()):
        thread.join(5)
    lc._windows.clear(); lc._windows.update(saved[0])
    lc._windows_unhydrated.clear(); lc._windows_unhydrated.update(saved[1])
    lc._assigned_zoho.clear(); dict.update(lc._assigned_zoho, saved[2])
    lc._details.clear(); lc._details.update(saved[3])
    with lc._state_lock:
        lc._assignment_state.clear(); lc._assignment_state.update(saved[4])
    lc._warm_failed.clear(); lc._warm_failed.update(saved[5])


# ---- purchase orders: stale data, budget, backoff, single flight, deadline --------------------------------------------------


class FakePO:
    def __init__(self):
        self.list_calls = 0
        self.behaviour = "ok"
        self.release = threading.Event()
        self.entered = threading.Event()

    def page(self, page=1, per_page=200):
        self.list_calls += 1
        self.entered.set()
        if self.behaviour == "timeout":
            timed_out()
        if self.behaviour == "budget":
            budget_blocked()
        if self.behaviour == "block":
            self.release.wait(5)
        return {"purchaseorders": [{"purchaseorder_id": "1", "purchaseorder_number": "PO-1026-01704", "status": "open", "date": "2026-10-10", "delivery_date": DAY,
                                    "branch_id": "4489499000001322444", "location_id": "4489499000064711027", "vendor_name": "Fresh Farms", "last_modified_time": "x"}],
                "page_context": {"has_more_page": False}}


class InlinePool:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture
def po(monkeypatch):
    purchase_orders.reset()
    fake = FakePO()
    monkeypatch.setattr(purchase_orders, "fetch_purchase_orders_page", fake.page)
    monkeypatch.setattr(purchase_orders, "fetch_purchase_order_detail", lambda pid: {"purchaseorder": {"purchaseorder_id": pid, "line_items": []}})
    monkeypatch.setattr(purchase_orders, "_pool", InlinePool())
    yield fake
    fake.release.set()
    deadline = time.monotonic() + 5
    while purchase_orders.is_refreshing() and time.monotonic() < deadline:
        time.sleep(0.01)
    purchase_orders.reset()


def call_po(**overrides):
    args = dict(date_from=DAY, date_to=DAY, search=None, statuses=None, assignment=None, page=1, per_page=25, db=None)
    args.update(overrides)
    return body_of(load_planning.list_purchase_orders(**args))


def expire_po_list():
    with purchase_orders._lock:
        purchase_orders._list["at"] = 0.0


def test_po_timeout_with_a_cached_list_answers_200_with_the_last_data_marked_stale(po):
    status, fresh = call_po()
    assert status == 200 and fresh["total"] == 1 and "stale" not in fresh
    expire_po_list()
    po.behaviour = "timeout"
    status, body = call_po()
    assert status == 200 and body["stale"] is True and body["stale_since"] and body["total"] == 1
    assert body["items"][0]["id"] == "PO:1"
    calls = po.list_calls
    status, again = call_po()  # inside the failure backoff: Zoho is not asked again
    assert status == 200 and again["stale"] is True and po.list_calls == calls


def test_po_timeout_with_nothing_in_memory_is_a_503_that_says_so(po):
    po.behaviour = "timeout"
    status, body = call_po()
    assert status == 503 and body == {"detail": "Zoho is not responding, showing last saved data", "code": "zoho_unavailable", "stale": True}
    calls = po.list_calls
    status, _ = call_po()  # backoff: no new pull for a minute
    assert status == 503 and po.list_calls == calls


def test_po_budget_block_is_a_clean_503_with_its_own_code_not_an_exception(po):
    po.behaviour = "budget"
    status, body = call_po()
    assert status == 503 and body["code"] == "zoho_budget" and "budget" in body["detail"] and body["stale"] is True
    status, body = body_of(load_planning.hard_refresh_purchase_orders())
    assert status == 503 and body["code"] == "zoho_budget"


def test_po_budget_block_with_a_cached_list_keeps_showing_it(po):
    call_po()
    expire_po_list()
    po.behaviour = "budget"
    status, body = call_po()
    assert status == 200 and body["stale"] is True and body["total"] == 1
    status, refresh = body_of(load_planning.hard_refresh_purchase_orders())
    assert status == 200 and refresh["stale"] is True and refresh["code"] == "zoho_budget" and refresh["count"] == 1


def test_two_concurrent_hard_refreshes_share_one_upstream_pull(po):
    call_po()
    po.behaviour = "block"
    po.entered.clear()
    before = po.list_calls
    results: list = []
    first = threading.Thread(target=lambda: results.append(body_of(load_planning.hard_refresh_purchase_orders())))
    first.start()
    assert po.entered.wait(5)
    second = threading.Thread(target=lambda: results.append(body_of(load_planning.hard_refresh_purchase_orders())))
    second.start()
    time.sleep(0.3)  # the second click arrives while the first pull is still running
    po.release.set()
    first.join(5); second.join(5)
    assert po.list_calls == before + 1
    assert [status for status, _ in results] == [200, 200] and all(body["count"] == 1 and "stale" not in body for _, body in results)


def test_a_cold_po_pull_past_the_deadline_answers_503_warming_and_fills_in_the_background(po, monkeypatch):
    monkeypatch.setattr(zoho_resilience, "REQUEST_DEADLINE_SECONDS", 0.2)
    po.behaviour = "block"
    started = time.monotonic()
    status, body = call_po()
    assert status == 503 and body["code"] == "warming" and time.monotonic() - started < 2
    po.release.set()
    deadline = time.monotonic() + 5
    while purchase_orders.is_refreshing() and time.monotonic() < deadline:
        time.sleep(0.01)
    calls = po.list_calls
    status, body = call_po()
    assert status == 200 and body["total"] == 1 and po.list_calls == calls  # filled by the background pull, no new Zoho call


def test_a_slow_po_refresh_with_a_cached_list_answers_from_memory_within_the_deadline(po, monkeypatch):
    call_po()
    expire_po_list()
    monkeypatch.setattr(zoho_resilience, "REQUEST_DEADLINE_SECONDS", 0.2)
    po.behaviour = "block"
    started = time.monotonic()
    status, body = call_po()
    assert status == 200 and body["stale"] is True and body["total"] == 1 and time.monotonic() - started < 2


# ---- sales orders: the Acknowledged view ----------------------------------------------------------------------------------


@pytest.fixture
def sales(monkeypatch, clean_lc):
    """The wide window is in memory (one SO shipping today); the Acknowledged view is a fake."""
    today = datetime.now(lc.PHT).date()
    row = lc._build_transient(so_record("so-1", today))
    with lc._window_lock:
        lc._store_window_locked(lc._windows, lc._wide_key(), {"so-1": row})
    view = SimpleNamespace(calls=0, behaviour="ok", has_more=False, release=threading.Event(), entered=threading.Event())

    def customview(view_id, page=1, per_page=200):
        view.calls += 1
        view.entered.set()
        if view.behaviour == "timeout":
            timed_out()
        if view.behaviour == "budget":
            budget_blocked()
        if view.behaviour == "block":
            view.release.wait(5)
        return {"salesorders": [{"salesorder_id": f"ack-{page}"}], "page_context": {"has_more_page": view.has_more}}

    monkeypatch.setattr(load_planning, "fetch_sales_orders_by_customview", customview)
    monkeypatch.setattr(load_planning, "stock_for_orders_cached", lambda rows: ({}, 0))
    with load_planning._ack_lock:
        load_planning._ack_cache.update(ids=None, at=0.0, failed_at=0.0, failed_since=None, error=None)
    load_planning._ack_overrides.clear()
    yield view, today
    view.release.set()
    with load_planning._ack_lock:
        load_planning._ack_cache.update(ids=None, at=0.0, failed_at=0.0, failed_since=None, error=None)


def inventory(day: date):
    return body_of(load_planning.list_sales_orders(date_from=day.isoformat(), date_to=day.isoformat(), status=None, search=None, assignment=None, cities=None,
                                                   vehicle=None, customer=None, delivery_status=None, branches=None, order_types=None, page=1, per_page=100, db=SimpleNamespace()))


def test_sales_orders_zoho_timeout_with_a_cached_ack_set_serves_the_window_marked_stale(sales):
    view, today = sales
    status, fresh = inventory(today)
    assert status == 200 and fresh["total"] == 1 and "stale" not in fresh
    with load_planning._ack_lock:
        load_planning._ack_cache["at"] = 0.0  # TTL lapsed
    view.behaviour = "timeout"
    status, body = inventory(today)
    assert status == 200 and body["stale"] is True and body["stale_since"] and body["total"] == 1
    calls = view.calls
    status, body = inventory(today)  # failure backoff: the view is not re-read on every request
    assert status == 200 and body["stale"] is True and view.calls == calls


def test_sales_orders_with_nothing_to_fall_back_on_is_a_503_stale_json(sales):
    view, today = sales
    view.behaviour = "timeout"
    status, body = inventory(today)
    assert status == 503 and body["stale"] is True and body["detail"] == "Zoho is not responding, showing last saved data"


def test_sales_orders_budget_block_is_a_clean_503_with_its_own_code(sales):
    view, today = sales
    view.behaviour = "budget"
    status, body = inventory(today)
    assert status == 503 and body["code"] == "zoho_budget" and "budget" in body["detail"]


def test_concurrent_requests_share_one_acknowledged_view_read(sales):
    view, today = sales
    view.behaviour = "block"
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(load_planning._ack_snapshot())) for _ in range(4)]
    threads[0].start()
    assert view.entered.wait(5)
    for thread in threads[1:]:
        thread.start()
    time.sleep(0.3)
    view.release.set()
    for thread in threads:
        thread.join(5)
    assert view.calls == 1 and len(results) == 4 and all(ids == {"ack-1"} for ids, _ in results)


def test_the_acknowledged_view_page_walk_is_capped(sales, monkeypatch):
    view, _ = sales
    view.has_more = True  # a view that never says "last page"
    monkeypatch.setattr(load_planning, "ACK_VIEW_MAX_PAGES", 5)
    ids = load_planning._fetch_acknowledged_ids_from_zoho()
    assert view.calls == 5 and len(ids) == 5


# ---- capped caches ----------------------------------------------------------------------------------------------------------


def test_window_cache_is_capped_and_never_drops_the_wide_window(clean_lc, monkeypatch):
    monkeypatch.setattr(lc, "_pull_window", lambda start, end: {f"{start}-{n}": so_record(f"{start}-{n}", start) for n in range(20)})
    monkeypatch.setattr(lc, "_hydrate_details", lambda raw: None)
    with lc._window_lock:
        lc._store_window_locked(lc._windows, lc._wide_key(), {})
    first = date(2026, 1, 1)
    gc.collect()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        for n in range(8):
            lc.get_window(first + timedelta(days=n), first + timedelta(days=n))
        gc.collect()
        after_8 = tracemalloc.get_traced_memory()[0]
        for n in range(8, 48):
            lc.get_window(first + timedelta(days=n), first + timedelta(days=n))
            assert len(lc._windows) <= lc.WINDOW_CACHE_MAX
        gc.collect()
        after_48 = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    assert lc._wide_key() in lc._windows and len(lc._windows) == lc.WINDOW_CACHE_MAX
    assert after_48 - after_8 < (after_8 - base) * 0.5  # 40 more distinct windows, no growth beyond the cap


def test_idle_non_wide_windows_expire_but_the_wide_window_never_does(clean_lc):
    old = time.monotonic() - lc.WINDOW_TTL_SECONDS - 1
    lc._windows[lc._wide_key()] = (old, {})
    lc._windows[("2026-01-01", "2026-01-01")] = (old, {})
    with lc._window_lock:
        lc._prune_windows_locked(lc._windows)
    assert list(lc._windows) == [lc._wide_key()]


def test_unhydrated_reads_do_not_hydrate_and_are_kept_apart(clean_lc, monkeypatch):
    hydrated: list = []
    monkeypatch.setattr(lc, "_pull_window", lambda start, end: {"r1": so_record("r1", start)})
    monkeypatch.setattr(lc, "_hydrate_details", lambda raw: hydrated.append(raw))
    far = date(2026, 1, 5)
    rows = lc.get_window(far, far, hydrate=False)
    assert [r.id for r in rows] == ["r1"] and hydrated == []
    assert (far.isoformat(), far.isoformat()) in lc._windows_unhydrated and (far.isoformat(), far.isoformat()) not in lc._windows


def test_cities_never_pull_a_second_window(clean_lc, monkeypatch):
    today = datetime.now(lc.PHT).date()
    with lc._window_lock:
        lc._store_window_locked(lc._windows, lc._wide_key(), {"c1": lc._build_transient(so_record("c1", today, city="Makati"))})
    monkeypatch.setattr(lc, "_pull_window", lambda *a: (_ for _ in ()).throw(AssertionError("no Zoho pull for city options")))
    assert load_planning.list_sales_order_cities(date_from=None, date_to=None) == {"cities": ["Makati"]}
    assert len(lc._windows) == 1


def test_assigned_zoho_drops_the_oldest_unassigned_rows_but_never_an_assigned_one(clean_lc, monkeypatch):
    monkeypatch.setattr(lc, "ASSIGNED_ZOHO_MAX", 5)
    with lc._state_lock:
        lc._assignment_state["kept"] = {"assignment_status": "assigned"}
    lc._assigned_zoho["kept"] = SimpleNamespace(id="kept")
    for n in range(12):
        lc._assigned_zoho[f"drawer-{n}"] = SimpleNamespace(id=f"drawer-{n}")
    assert "kept" in lc._assigned_zoho and len(lc._assigned_zoho) == 6
    assert [k for k in lc._assigned_zoho if k != "kept"] == [f"drawer-{n}" for n in range(7, 12)]


def test_assigned_zoho_expires_idle_unassigned_rows(clean_lc, monkeypatch):
    lc._assigned_zoho["old"] = SimpleNamespace(id="old")
    lc._assigned_zoho._at["old"] = time.monotonic() - lc.ASSIGNED_ZOHO_TTL_SECONDS - 1
    lc._assigned_zoho["new"] = SimpleNamespace(id="new")
    assert list(lc._assigned_zoho) == ["new"]


def test_detail_cache_cap_also_applies_to_acknowledge(clean_lc, monkeypatch):
    monkeypatch.setattr(lc, "DETAIL_CACHE_MAX", 3)
    for n in range(5):
        lc.mark_acknowledged(f"so-{n}", True)
    assert list(lc._details) == ["so-2", "so-3", "so-4"]
    assert lc.cached_detail_has_acknowledged_sub_status("so-4")


def test_purchase_and_transfer_order_detail_caches_are_capped_and_drop_orders_gone_from_the_list(monkeypatch):
    purchase_orders.reset()
    transfer_orders.reset()
    monkeypatch.setattr(purchase_orders, "DETAIL_CACHE_MAX", 3)
    monkeypatch.setattr(transfer_orders, "DETAIL_CACHE_MAX", 3)
    monkeypatch.setattr(purchase_orders, "fetch_purchase_order_detail", lambda pid: {"purchaseorder": {"purchaseorder_id": pid, "line_items": []}})
    try:
        for n in range(6):
            purchase_orders.fetch_detail(str(n))
            transfer_orders._store_detail(str(n), "m", {"line_items": []})
        assert list(purchase_orders._details) == ["3", "4", "5"] and list(transfer_orders._details) == ["3", "4", "5"]
        with purchase_orders._lock:
            purchase_orders._failed.update({"old": time.monotonic() - 3600, "5": time.monotonic()})
            purchase_orders._prune_details_locked({"5"})
        assert list(purchase_orders._details) == ["5"] and list(purchase_orders._failed) == ["5"]
    finally:
        purchase_orders.reset()
        transfer_orders.reset()


def test_dispatch_dashboard_caches_are_capped():
    saved = (dict(pipeline._zoho_detail_cache), dict(pipeline._zoho_date_cache), dict(pipeline._dashboard_last_good))
    try:
        with pipeline._zoho_cache_lock:
            for n in range(pipeline.DETAIL_CACHE_MAX + 50):
                pipeline._remember(pipeline._zoho_detail_cache, f"so-{n}", ("m", {}), pipeline.DETAIL_CACHE_MAX)
            for n in range(30):
                pipeline._remember(pipeline._dashboard_last_good, f"2026-10-{n:02d}", {}, pipeline.DASHBOARD_DATES_MAX)
        assert len(pipeline._zoho_detail_cache) == pipeline.DETAIL_CACHE_MAX and "so-0" not in pipeline._zoho_detail_cache
        assert len(pipeline._dashboard_last_good) == 14 and "2026-10-29" in pipeline._dashboard_last_good
    finally:
        for cache, old in zip((pipeline._zoho_detail_cache, pipeline._zoho_date_cache, pipeline._dashboard_last_good), saved):
            cache.clear(); cache.update(old)


def test_a_remembered_background_failure_holds_no_traceback(clean_lc, monkeypatch):
    monkeypatch.setattr(lc, "_pull_window", budget_blocked)
    with pytest.raises(lc.WindowWarming):
        lc.get_window(date(2026, 1, 1), date(2026, 1, 1), block=False)
    for thread in list(lc._warming.values()):
        thread.join(5)
    (_, stored), = lc._warm_failed.values()
    assert isinstance(stored, ZohoError) and stored.__traceback__ is None and stored.__cause__ is None
    assert zoho_resilience.is_budget_error(stored)
    with pytest.raises(ZohoError) as caught:
        lc.get_window(date(2026, 1, 1), date(2026, 1, 1), block=False)
    assert "budget" in str(caught.value) and stored.__traceback__ is None  # re-raising never re-attaches a traceback to the stored one


def test_a_cold_window_answers_from_a_covering_cached_window_marked_stale(clean_lc, monkeypatch):
    monkeypatch.setattr(lc, "_pull_window", budget_blocked)
    day = date(2026, 1, 3)
    lc._windows[("2026-01-01", "2026-01-10")] = (time.monotonic(), {"r": lc._build_transient(so_record("r", day))})
    with zoho_resilience.track_stale() as stale:
        rows = lc.get_window(day, day, block=False)
    assert [r.id for r in rows] == ["r"] and stale["stale"] is True


# ---- zoho_acquisition: no deep copy when nobody waits ------------------------------------------------------------------------


def test_acquire_does_not_deep_copy_a_response_nobody_waits_for(monkeypatch):
    copies: list = []
    real = zoho_acquisition.deepcopy
    monkeypatch.setattr(zoho_acquisition, "deepcopy", lambda value: (copies.append(1), real(value))[1])
    value = zoho_acquisition.acquire(("solo",), lambda logical_id: {"big": list(range(100))})
    assert value["big"][-1] == 99 and copies == []


def test_acquire_still_gives_each_waiter_its_own_copy():
    release = threading.Event()
    entered = threading.Event()
    payload = {"line_items": [1, 2, 3]}

    def loader(logical_id):
        entered.set()
        release.wait(5)
        return payload

    results: list = []
    owner = threading.Thread(target=lambda: results.append(("owner", zoho_acquisition.acquire(("shared",), loader))))
    owner.start()
    assert entered.wait(5)
    waiter = threading.Thread(target=lambda: results.append(("waiter", zoho_acquisition.acquire(("shared",), loader))))
    waiter.start()
    time.sleep(0.2)
    release.set()
    owner.join(5); waiter.join(5)
    got = dict(results)
    assert got["owner"] is payload and got["waiter"] == payload and got["waiter"] is not payload
