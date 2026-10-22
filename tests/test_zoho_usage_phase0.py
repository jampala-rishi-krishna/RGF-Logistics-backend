"""Phase 0 request-count contracts. All Zoho transport calls are mocked; no providers.

Counts are logical HTTP requests before transport retries. The actual client helper,
cache and router code runs; only _request_with_feature is replaced by a counting fake.
conftest.py also refuses external network access and the production DB.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from auth.dependencies import get_current_user
from routers import fleet, load_planning, pipeline, reports
from services import item_detail_cache as items, live_sales_order_cache as lc
from services import warehouse_stock, zoho_acquisition, zoho_client, purchase_orders, transfer_orders


TODAY = datetime.now(lc.PHT).date()


def record(oid, ship=TODAY, modified="v1"):
    return {"salesorder_id": oid, "salesorder_number": f"SO-{oid}",
            "customer_name": "Test", "branch_id": "4489499000001322444",
            "shipment_date": ship.isoformat(), "status": "confirmed",
            "last_modified_time": modified,
            "line_items": [{"item_id": f"item-{oid}", "quantity": 1, "unit": "case"}]}


@pytest.fixture
def provider(monkeypatch):
    calls = []

    def request(method, path, params, feature):
        calls.append((method, path, deepcopy(params)))
        if path == "salesorders":
            return {"salesorders": [], "page_context": {"has_more_page": False, "search_criteria": [{"column_name": "shipment_date"}]}}
        if path.startswith("salesorders/"):
            return {"salesorder": record(path.split("/")[1])}
        if path == "items":
            return {"items": [{"item_id": oid, "package_details": {"weight": 2, "weight_unit": "kg"}}
                              for oid in params["item_ids"].split(",")]}
        if path.startswith("purchaseorders/"):
            return {"purchaseorder": {"purchaseorder_id": path.split("/")[1], "purchaseorder_number": "PO-TEST", "vendor_id": "vendor-not-cached", "line_items": [], "delivery_date": TODAY.isoformat()}}
        if path.startswith("transferorders/"):
            return {"transfer_order": {"transfer_order_id": path.split("/")[1], "transfer_order_number": "TO-TEST", "date": TODAY.isoformat(), "line_items": []}}
        return {path: [], "page_context": {"has_more_page": False}}

    monkeypatch.setattr(zoho_client, "_request_with_feature", request)
    monkeypatch.setattr(lc, "_assignment_state", {})
    monkeypatch.setattr(lc, "_assigned_zoho", lc._OrderRowCache())
    monkeypatch.setattr(lc, "_excluded_ids", {})
    monkeypatch.setattr(lc, "_windows", {})
    monkeypatch.setattr(lc, "_windows_unhydrated", {})
    monkeypatch.setattr(lc, "_details", {})
    monkeypatch.setattr(lc, "_observe_dates", lambda _: None)
    monkeypatch.setattr(reports, "_cache", {})
    monkeypatch.setattr(reports, "_report_inflight", {})
    items.invalidate()
    with items._lock:
        items._inflight.clear()
    yield calls
    items.invalidate()


def prepare_fleet(monkeypatch, cached=20):
    for i in range(20):
        oid = str(i)
        lc._assignment_state[oid] = {"assignment_status": "assigned", "vehicle_id": "TEST", "expected_shipment_date": TODAY}
        if i < cached:
            lc._assigned_zoho[oid] = lc._apply_assignment(lc._build_transient(record(oid)))
    db = MagicMock()
    db.get.return_value = None
    db.execute.return_value.scalars.return_value.all.return_value = []
    context = MagicMock()
    context.__enter__.return_value = db
    monkeypatch.setattr(fleet, "SessionLocal", lambda: context)
    monkeypatch.setattr(lc, "local_zoho_warmup_disabled", lambda: False)
    monkeypatch.setattr(fleet.zoho_usage, "is_working_time", lambda: True)
    # Any accidental wide-window path is a test failure, not a provider request.
    monkeypatch.setattr(lc, "refresh_shared_window", MagicMock(side_effect=AssertionError("wide refresh")))
    monkeypatch.setattr(lc, "get_assigned_snapshot", MagicMock(side_effect=AssertionError("window-enabled snapshot")))
    return db


@pytest.mark.parametrize("cached,expected", [(20, 0), (15, 5)])
@pytest.mark.parametrize("scheduled", [True, False])
def test_fleet_assigned_only_counts(provider, monkeypatch, cached, expected, scheduled):
    db = prepare_fleet(monkeypatch, cached)
    if scheduled:
        fleet.scheduled_fleet_sync()
    else:
        fleet.refresh_vehicles(force=True, db=db)
    assert len(provider) == expected
    assert {path for _, path, _ in provider} == {f"salesorders/{i}" for i in range(cached, 20)}
    assert all(method == "GET" for method, _, _ in provider)
    assert len(lc._assigned_zoho) == 20
    # Real row rendering must not fetch missing package weights or reload the window.
    vehicle = SimpleNamespace(plate_no="TEST", rated_capacity_kg=100)
    monkeypatch.setattr(fleet, "row_to_dict", lambda _: {"plate_no": "TEST"})
    result = fleet._vehicle_payload(vehicle, db, assigned_by_vehicle={"TEST": list(lc._assigned_zoho.values())})
    assert len(result["associated_sos"]) == 20
    assert len(provider) == expected


def test_fleet_render_does_not_reenter_default_snapshot(provider, monkeypatch):
    db = prepare_fleet(monkeypatch)
    vehicle = SimpleNamespace(plate_no="TEST", rated_capacity_kg=100)
    db.execute.return_value.scalars.return_value.all.return_value = [vehicle]
    monkeypatch.setattr(fleet, "row_to_dict", lambda _: {"plate_no": "TEST"})
    assert len(fleet.refresh_vehicles(db=db)[0]["associated_sos"]) == 20
    assert provider == []


def test_assigned_po_to_only_missing_ids_no_vendor_or_list_fanout(provider, monkeypatch):
    monkeypatch.setattr(purchase_orders, "_details", {})
    monkeypatch.setattr(purchase_orders, "_vendors", {})
    monkeypatch.setattr(transfer_orders, "_details", {})
    for oid in ("PO:po-phase0", "TO:to-phase0"):
        lc._assignment_state[oid] = {"assignment_status": "assigned", "vehicle_id": "TEST", "expected_shipment_date": TODAY}
    rows, failures = lc.get_assigned_snapshot_ex(include_window=False, statuses=fleet.ASSIGNED_ONLY)
    assert not failures
    assert len(rows) == 2
    assert {p for _, p, _ in provider} == {"purchaseorders/po-phase0", "transferorders/to-phase0"}
    assert len(provider) == 2
    lc.get_assigned_snapshot_ex(include_window=False, statuses=fleet.ASSIGNED_ONLY)
    assert len(provider) == 2
    # A later normal PO detail request still performs vendor enrichment.
    purchase_orders.fetch_detail("po-phase0")
    assert len(provider) == 4
    assert provider[-1][1] == "contacts/vendor-not-cached"


def test_so_refresh_only_selected_date_pages_and_reuses_slice(provider, monkeypatch):
    target = TODAY + timedelta(days=2)
    calls = provider
    def list_page(method, path, params, feature):
        calls.append((method, path, deepcopy(params)))
        assert path == "salesorders"
        assert params["shipment_date_start"] == params["shipment_date_end"] == target.isoformat()
        page = params["page"]
        return {"salesorders": [record(f"{page}-{i}", target) for i in range(200 if page == 1 else 1)],
                "page_context": {"has_more_page": page == 1, "search_criteria": [{"column_name": "shipment_date"}]}}
    monkeypatch.setattr(zoho_client, "_request_with_feature", list_page)
    # Details unchanged: count only the list pages, as this scenario specifies.
    for p, size in ((1, 200), (2, 1)):
        for i in range(size):
            oid = f"{p}-{i}"
            lc._details[oid] = ("v1", record(oid, target))
    load_planning._run_sales_order_sync(target, target)
    assert load_planning._refresh_state["error"] is None
    assert len(calls) == 2
    assert [c[2]["page"] for c in calls] == [1, 2]
    assert len(lc.get_window(target, target)) == 201
    assert len(calls) == 2  # next browser read cannot start the wide window


def test_date_slice_preserves_other_days_and_removes_stale_rows(provider, monkeypatch):
    other = TODAY + timedelta(days=1)
    key = tuple(d.isoformat() for d in lc._wide_bounds())
    lc._windows[key] = (lc.time.monotonic(), {"old": lc._build_transient(record("old")), "other": lc._build_transient(record("other", other))})
    assert lc.refresh_window(TODAY, TODAY) == 0
    assert set(lc._windows[key][1]) == {"other"}
    assert len(provider) == 1
    assert len(lc.get_window(TODAY, TODAY)) == 0
    assert len(provider) == 1


def test_so_refresh_without_dates_defaults_to_today_manila(provider):
    load_planning._run_sales_order_sync(None, None)
    assert len(provider) == 1
    assert provider[0][2]["shipment_date_start"] == TODAY.isoformat()
    assert provider[0][2]["shipment_date_end"] == TODAY.isoformat()


def test_100_items_without_stock_one_call_normal_ttl(provider, monkeypatch):
    ids = [str(i) for i in range(100)]
    class Inline:
        def submit(self, fn, *args):
            fn(*args)
    monkeypatch.setattr(items, "_pool", Inline())
    assert items.request_refresh(ids) == 100
    assert len(provider) == 1
    assert provider[0][1] == "items"
    assert items.request_refresh(ids) == 0
    for oid in ids:
        assert warehouse_stock.fetch_item_stock(oid, "4489499000001322444") == {"mets": None, "glacier": None}
    assert len(provider) == 1
    with items._lock:
        for oid, (at, data) in list(items._items.items()):
            items._items[oid] = (at - items.FRESH_SECONDS - 1, data)
    items.request_refresh(ids)
    assert len(provider) == 2  # one new batch only after normal TTL


def test_successful_batch_with_omitted_items_does_not_repeat(provider, monkeypatch):
    monkeypatch.setattr(zoho_client, "_request_with_feature", lambda *a: provider.append(a) or {"items": []})
    items._batch_refresh_worker(["a", "b"])
    assert items.request_refresh(["a", "b"]) == 0
    assert len(provider) == 1


def test_failed_batch_uses_backoff_without_single_fanout(provider, monkeypatch):
    def fail(*args):
        provider.append(args)
        raise zoho_client.ZohoError("mocked unavailable")
    monkeypatch.setattr(zoho_client, "_request_with_feature", fail)
    items._batch_refresh_worker(["a", "b"])
    assert items.request_refresh(["a", "b"]) == 0
    assert len(provider) == 1
    assert items.get_cached("a") == (None, False)


def test_report_section_repeated_refresh_zero_calls(provider, monkeypatch):
    monkeypatch.setattr(reports.time, "sleep", lambda _: None)
    first = reports.get_rgf_logistics_report(section="invoices")
    assert len(provider) == 1
    assert provider[0][1] == "invoices"
    assert reports.get_rgf_logistics_report(section="invoices") == first
    assert len(provider) == 1
    first["errors"]["mutated"] = True
    assert "mutated" not in reports.get_rgf_logistics_report(section="invoices")["errors"]


def test_full_report_ttl_15_minutes_and_admin_force(provider, monkeypatch):
    monkeypatch.setattr(reports.time, "sleep", lambda _: None)
    clock = [1000.0]
    monkeypatch.setattr(reports.time, "time", lambda: clock[0])
    reports.get_rgf_logistics_report()
    initial = len(provider)
    assert initial > 0
    clock[0] += 899
    reports.get_rgf_logistics_report()
    assert len(provider) == initial
    clock[0] += 1
    reports.get_rgf_logistics_report()
    assert len(provider) == 2 * initial
    reports.get_rgf_logistics_report(force=True, current_user=SimpleNamespace(role="admin"))
    assert len(provider) == 3 * initial


@pytest.mark.parametrize("role", ["dispatcher", "warehouse", "planner", "sales"])
def test_nonadmin_force_denied_before_fetch(provider, role):
    with pytest.raises(HTTPException) as exc:
        reports.get_rgf_logistics_report(force=True, section="invoices", current_user=SimpleNamespace(role=role))
    assert exc.value.status_code == 403
    assert provider == []


def test_report_role_dependency_over_http(provider, monkeypatch):
    monkeypatch.setattr(reports.time, "sleep", lambda _: None)
    app = FastAPI()
    app.include_router(reports.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="dispatcher")
    client = TestClient(app)
    assert client.get("/api/reports/rgf-logistics?force=true&section=invoices").status_code == 403
    assert provider == []
    assert client.get("/api/reports/rgf-logistics?section=invoices").status_code == 200
    assert len(provider) == 1
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="admin")
    assert client.get("/api/reports/rgf-logistics?force=true&section=invoices").status_code == 200
    assert len(provider) == 2


@pytest.mark.parametrize("force", [False, True])
def test_yesterday_dashboard_zero_calls_preserves_history(provider, monkeypatch, force):
    yesterday = TODAY - timedelta(days=1)
    history = SimpleNamespace(id="hist", salesorder_number="SO-HISTORY", customer_name="Past customer", expected_shipment_date=yesterday,
                              vehicle_id="YESTERDAY", driver_id=None, helper_ids=[], manifest_id=None, order_status="confirmed",
                              shipment_status=None, delivery_status="Delivered", salesperson_name="Past salesperson", shipping_address={"city": "Manila"})
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = [history]
    monkeypatch.setattr(pipeline.fleet_static_cache, "vehicles", lambda: [])
    monkeypatch.setattr(lc, "invalidate_windows", MagicMock(side_effect=AssertionError("history must not invalidate live data")))
    monkeypatch.setattr(lc, "invalidate_assigned_zoho", MagicMock(side_effect=AssertionError("history must not invalidate live data")))
    lc._assignment_state["hist"] = {"vehicle_id": "TODAY"}
    result = pipeline.dispatch_dashboard(yesterday.isoformat(), refresh=force, db=db)
    assert provider == []
    assert result["zoho_api_calls"] == 0
    assert result["orders"][0]["truck_plate"] == "YESTERDAY"
    assert result["orders"][0]["status"] == "Delivered"
    assert "expected_shipment_date" in str(db.execute.call_args.args[0].whereclause)


def test_unacknowledged_filter_three_page_fallback_warning(provider, monkeypatch, caplog):
    def request(method, path, params, feature):
        provider.append((method, path, deepcopy(params)))
        if "shipment_date_start" in params:
            return {"salesorders": [], "page_context": {"search_criteria": []}}
        page = params["page"]
        return {"salesorders": [record(f"{page}-{i}") for i in range(200)], "page_context": {"has_more_page": True}}
    monkeypatch.setattr(zoho_client, "_request_with_feature", request)
    result = lc._pull_window(TODAY, TODAY)
    assert len(provider) == 4  # 1 unsupported-filter probe + at most 3 fallback pages
    assert len(result) == 600
    assert [c[2]["page"] for c in provider[1:]] == [1, 2, 3]
    assert provider[1][2]["date_start"] == (TODAY - timedelta(days=90)).isoformat()
    assert "pages=3 rows_scanned=600 rows_matched=600" in caplog.text
    assert "potentially_truncated=True" in caplog.text


def test_fallback_early_exit_warning_counts(provider, monkeypatch, caplog):
    def request(method, path, params, feature):
        provider.append((method, path, deepcopy(params)))
        return {"salesorders": [record("one")], "page_context": {"has_more_page": False}}
    monkeypatch.setattr(zoho_client, "_request_with_feature", request)
    assert len(lc._pull_window(TODAY, TODAY)) == 1
    assert len(provider) == 2
    assert "pages=1 rows_scanned=1 rows_matched=1" in caplog.text
    assert "potentially_truncated=False" in caplog.text
