from datetime import date, datetime, timezone

from tests.sqlite_helpers import make_engine, make_session_factory


def _record(order_id="SO-1", modified="2026-10-23T01:00:00+00:00", **extra):
    value = {
        "salesorder_id": order_id,
        "salesorder_number": order_id,
        "shipment_date": "2026-10-23",
        "branch_id": "RGC",
        "last_modified_time": modified,
        "status": "confirmed",
        "current_sub_status": "acknowledged",
        "line_items": [{"item_id": "I-1", "name": "Rice", "quantity": 2, "warehouse": {"id": "W-1"}}],
    }
    value.update(extra)
    return value


def test_upsert_is_timestamp_and_payload_aware(monkeypatch):
    import services.zoho_so_live_cache as cache
    from models.zoho_so_live_cache import ZohoSoLiveCache

    engine = make_engine(ZohoSoLiveCache.__table__)
    Session = make_session_factory(engine)
    monkeypatch.setattr(cache, "SessionLocal", Session)

    assert cache.upsert_snapshot(_record(), source="api") is True
    assert cache.upsert_snapshot(_record(), source="api") is False
    assert cache.upsert_snapshot(_record(modified="2026-10-22T01:00:00+00:00"), source="api") is False
    assert cache.upsert_snapshot(_record(current_sub_status="closed"), source="api") is True
    with Session() as db:
        row = db.get(ZohoSoLiveCache, "SO-1")
        assert row.snapshot["line_items"][0]["warehouse_id"] == "W-1"
        assert row.snapshot["current_sub_status"] == "closed"


def test_partial_lock_event_merges_without_erasing_live_copy(monkeypatch):
    import services.zoho_so_live_cache as cache
    from models.zoho_so_live_cache import ZohoSoLiveCache

    engine = make_engine(ZohoSoLiveCache.__table__)
    Session = make_session_factory(engine)
    monkeypatch.setattr(cache, "SessionLocal", Session)
    assert cache.upsert_snapshot(_record(), source="webhook") is True
    assert cache.upsert_snapshot({"salesorder_id": "SO-1", "lock_details": {"is_locked": True}}, source="webhook") is True
    with Session() as db:
        row = db.get(ZohoSoLiveCache, "SO-1")
        assert row.shipment_date == date(2026, 10, 23)
        assert row.snapshot["line_items"]
        assert row.snapshot["lock_details"]["is_locked"] is True


def test_window_read_reports_rows_and_approx_bytes(monkeypatch):
    import services.zoho_so_live_cache as cache
    from models.zoho_so_live_cache import ZohoSoLiveCache

    engine = make_engine(ZohoSoLiveCache.__table__)
    Session = make_session_factory(engine)
    monkeypatch.setattr(cache, "SessionLocal", Session)
    cache.upsert_snapshot(_record(), source="api")
    rows, approx_bytes = cache.load_window(date(2026, 10, 22), date(2026, 10, 24), ("RGC",))
    assert len(rows) == 1
    assert approx_bytes > 0


def test_startup_seed_flag_false_is_exact_noop(monkeypatch):
    import services.live_sales_order_cache as live
    monkeypatch.setenv("ZOHO_STARTUP_SEED", "false")
    called = []
    monkeypatch.setattr(live, "_pull_delta_since", lambda *args: called.append(args))
    result = live.startup_seed()
    assert result["enabled"] is False
    assert called == []


def test_startup_seed_reads_cache_then_only_changed_delta(monkeypatch):
    import services.live_sales_order_cache as live
    import services.zoho_so_live_cache as cache

    monkeypatch.setenv("ZOHO_STARTUP_SEED", "true")
    stored = [_record(f"SO-{i}", "2026-10-22T01:00:00+00:00") for i in range(300)]
    changed = {f"SO-{i}": _record(f"SO-{i}", "2026-10-23T02:00:00+00:00") for i in range(5)}
    calls = {"delta": 0, "g4": 0, "writes": 0}
    monkeypatch.setattr(cache, "load_window", lambda *args: (stored, 900_000))
    monkeypatch.setattr(cache, "max_modified", lambda rows: datetime(2026, 10, 22, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(cache, "cleanup", lambda today: 0)
    monkeypatch.setattr(cache, "upsert_snapshot", lambda *args, **kwargs: calls.__setitem__("writes", calls["writes"] + 1) or True)
    monkeypatch.setattr(live, "_seed_row", lambda record: None)
    monkeypatch.setattr(live, "_pull_delta_since", lambda *args: calls.__setitem__("delta", calls["delta"] + 1) or changed)
    monkeypatch.setattr(live, "_hydrate_details", lambda rows: calls.__setitem__("g4", len(rows)))
    result = live.startup_seed()
    assert result["rows"] == 300
    assert calls == {"delta": 1, "g4": 5, "writes": 5}


def test_startup_seed_write_failures_never_crash_startup(monkeypatch, caplog):
    import services.live_sales_order_cache as live
    import services.zoho_so_live_cache as cache

    monkeypatch.setenv("ZOHO_STARTUP_SEED", "true")
    changed = {f"SO-{i}": _record(f"SO-{i}", "2026-10-23T02:00:00+00:00") for i in range(3)}
    seeded = []
    boom = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("neon down"))
    monkeypatch.setattr(cache, "load_window", lambda *args: ([], 0))
    monkeypatch.setattr(cache, "max_modified", lambda rows: None)
    monkeypatch.setattr(cache, "upsert_snapshot", boom)
    monkeypatch.setattr(cache, "cleanup", boom)
    monkeypatch.setattr(live, "_seed_row", seeded.append)
    monkeypatch.setattr(live, "_pull_delta_since", lambda *args: changed)
    monkeypatch.setattr(live, "_hydrate_details", lambda rows: None)
    with caplog.at_level("WARNING"):
        result = live.startup_seed()
    assert result["enabled"] is True and result["zoho_details"] == 3
    assert len(seeded) == 3  # every delta row still reaches memory
    assert "[ZOHO_STARTUP_SEED] write failed rows=3" in caplog.text
    assert "[ZOHO_STARTUP_SEED] write failed cleanup" in caplog.text


def test_startup_seed_neon_failure_falls_back_with_warning(monkeypatch, caplog):
    import services.live_sales_order_cache as live
    import services.zoho_so_live_cache as cache

    monkeypatch.setenv("ZOHO_STARTUP_SEED", "true")
    monkeypatch.setattr(cache, "load_window", lambda *args: (_ for _ in ()).throw(RuntimeError("neon down")))
    fallback = []
    monkeypatch.setattr(live, "refresh_shared_window", lambda **kwargs: fallback.append(kwargs) or 12)
    with caplog.at_level("WARNING"):
        result = live.startup_seed()
    assert result["fallback"] is True
    assert fallback == [{"force_full": True}]
    assert "Neon unavailable" in caplog.text

