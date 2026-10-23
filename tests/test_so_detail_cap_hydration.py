"""Regression (2026-10-23 prod): when the so_detail_reconcile cap is used up, the real zoho_client guard refuses
every SO detail call. A refresh must keep details already in memory instead of serving list-only rows
("Details unavailable" / dashes in every Load Planning tab). No network: the guard raises before any call."""
import glob, json
from datetime import timedelta
from services import live_sales_order_cache as live, branches, app_settings, zoho_usage

def setup(monkeypatch, cap):
    for name in ("_details", "_webhook_copies", "_windows", "_windows_unhydrated", "_assigned_zoho"):
        monkeypatch.setattr(live, name, {})
    monkeypatch.setattr(live, "_observe_dates", lambda r: None)
    monkeypatch.setattr(branches, "is_allowed", lambda r: True)
    monkeypatch.setattr(branches, "filter_allowed", lambda rs: list(rs))
    real = app_settings.get_int
    monkeypatch.setattr(app_settings, "get_int", lambda k: cap if k == "ZOHO_CAP_SO_DETAIL_RECONCILE" else real(k))
    start, _ = live._wide_bounds(); day = start + timedelta(days=1)
    details = {}
    for i, p in enumerate(sorted(glob.glob("tests/fixtures/zoho_webhooks/so_*.json"))):
        if "closed" in p: continue
        r = json.load(open(p, encoding="utf-8")); r = dict(r.get("salesorder", r))
        r.update(salesorder_id=f"S{i}", salesorder_number=f"SO26-9{i:03d}", shipment_date=day.isoformat(), status="confirmed", last_modified_time=f"2026-10-23T1{i%10}:00:00+0800")
        details[r["salesorder_id"]] = r
    listing = [{k: v for k, v in r.items() if k not in ("line_items", "shipping_address")} for r in details.values()]
    page = {"salesorders": listing, "raw_count": len(listing), "page_context": {"has_more_page": False, "search_criteria": [{"column_name": "shipment_date"}]}}
    monkeypatch.setattr(live, "fetch_sales_orders_by_shipment_date", lambda *a, **k: page)
    return day, details, page

def show(day):
    rows = live.cached_rows_overlapping(day, day)
    empty = sum(not (r.raw_json or {}).get("line_items") for r in rows)
    print(f"rows={len(rows)} rows_without_details={empty}")
    return rows, empty

def test_cold_start_with_detail_cap_reached_has_no_lines_until_cap_raised(monkeypatch):
    """Exact prod symptom: cap used up, real zoho_client guard refuses every detail call."""
    day, details, _ = setup(monkeypatch, cap=0)   # 0 => used(>=0) >= cap: the cap is reached
    # live.fetch_sales_order_detail is the REAL zoho_client function; the guard raises before any network/token call.
    live.refresh_shared_window(force_full=True)
    print("\n[cold start, cap reached]"); rows, empty = show(day)
    assert rows and empty == len(rows)

def test_capped_refresh_keeps_details_already_in_memory(monkeypatch):
    day, details, page = setup(monkeypatch, cap=0)
    import services.zoho_client as zc
    monkeypatch.setattr(live, "fetch_sales_order_detail", lambda rid: {"salesorder": details[rid]})
    live.refresh_shared_window(force_full=True)
    print("\n[warm fill]"); show(day)
    for r in page["salesorders"]:   # every SO edited in Zoho -> needs a new detail call
        r["last_modified_time"] = r["last_modified_time"].replace("+0800", "+0801")
    monkeypatch.setattr(live, "fetch_sales_order_detail", zc.fetch_sales_order_detail)  # real guard again
    live.refresh_shared_window(force_full=True)
    print("[refresh after cap reached]"); rows, empty = show(day)
    assert empty == 0, "a capped refresh wiped details that were already in memory"
