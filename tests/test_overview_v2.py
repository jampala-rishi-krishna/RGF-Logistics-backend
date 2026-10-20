"""Driver Overview v2 (CONTRACT.md): driver_step / tile / attention_flags (pure), /trips rows + tile_counts + orders_line + not_assigned,
search, filters, the trip drawer additions, /hardcopies + /issues filters, export with q / tile. SQLite fixtures; zero Zoho."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from models.trip import DriverIssue, FormTemplateItem, InvoiceHandoverItem, TripEvent
from services import day_model, overview, trip_events
from tests import trip_test_support as t
from tests.test_overview import env, seed  # noqa: F401  (env: the shared Driver Overview fixture)

NOW = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)


# ---- pure helpers ------------------------------------------------------------------------------------------------------------


def stop(so_id, seq, status="pending", outcome=None, checkin=None, checkout=None, far=None, distance=None, gps=True, answers=None, template=2):
    point = {"lat": 14.5, "lng": 121.0} if gps else {"lat": None, "lng": None, "reason": "denied"}
    return SimpleNamespace(so_id=so_id, sequence=seq, status=status, outcome=outcome, checkin_at=checkin, checkout_at=checkout, checkin_far=far, checkin_distance_m=distance,
                           checkin_gps=point if checkin else None, checkout_gps=point if checkout else None, form_answers=answers, form_template_id=template if checkout else None)


def facts(n=3, *, status=None, acks=0, stops=(), items=(), handover=False, started=None):
    orders = [day_model.OrderFacts(f"s{i}", f"SO-{i}", f"Cust {i}", 1.0, True, None) for i in range(1, n + 1)]
    trip = SimpleNamespace(status=status, started_at=started) if status else None
    stop_map = {s.so_id: s for s in stops}
    ack_map = {f"s{i}": object() for i in range(1, acks + 1)}
    f = day_model.BatchFacts(batch_id="X", date=date(2026, 10, 15), driver_id=9, helper_ids=[17], vehicle="NAA1", orders=orders, trip=trip, acks=ack_map, stops=stop_map,
                             handover=SimpleNamespace(id=1) if handover else None, items=list(items), **day_model.progress([o.so_id for o in orders], trip, ack_map, stop_map))
    return f


def item(so_id, status="pending", handed=True):
    return SimpleNamespace(so_id=so_id, officer_status=status, handed_over=handed, invoice_no=None)


def ev(event_type, minutes_ago, *, role="driver", so_id=None, gps=True, result=None, id_=1):
    return SimpleNamespace(id=id_, event_type=event_type, role=role, so_id=so_id, client_ts=NOW - timedelta(minutes=minutes_ago), gps={"lat": 14.5, "lng": 121.0} if gps else None, result=result)


def test_driver_step_every_state():
    at = NOW - timedelta(minutes=5)
    assert day_model.driver_step(facts(3, acks=1)) == {"step": 1, "label_key": "waiting_crew", "sub": {"acknowledged": 1, "total": 3, "problems": 0}}
    assert day_model.driver_step(facts(3, acks=3))["label_key"] == "ready"
    assert day_model.driver_step(facts(0))["label_key"] == "waiting_crew"  # nothing to load is never "ready"
    assert day_model.driver_step(facts(2, status="pending", acks=2))["label_key"] == "ready"  # a pending trip row = still step 1
    on = facts(3, status="on_trip", acks=3, stops=[stop("s1", 1, "checked_out", "Delivered", at, at), stop("s2", 2, "checked_in", checkin=at), stop("s3", 3)])
    assert day_model.driver_step(on) == {"step": 2, "label_key": "on_road", "sub": {"done": 1, "total": 3, "current": 2, "current_so": "SO-2", "here_since": at.isoformat(), "problems": 0}}
    between = facts(3, status="on_trip", acks=3, stops=[stop("s1", 2, "checked_out", "Failed / Returned", at, at), stop("s2", 1, "checked_out", "Partially delivered", at, at), stop("s3", 3)])
    assert day_model.driver_step(between)["sub"] == {"done": 2, "total": 3, "current": 3, "current_so": "SO-3", "here_since": None, "problems": 2}  # delivery order = stop sequence
    all_done = facts(1, status="on_trip", acks=1, stops=[stop("s1", 1, "checked_out", "Delivered", at, at)])
    assert day_model.driver_step(all_done)["sub"]["current"] is None
    legacy = facts(2, status="returned", acks=2, stops=[stop("s1", 1, "checked_out", "Delivered", at, at), stop("s2", 2, "checked_out", "Delivered", at, at)])
    assert day_model.driver_step(legacy) == {"step": 3, "label_key": "invoices_due", "sub": {"received": 0, "total_invoices": 2, "missing": 0, "discrepancy": 0, "problems": 0}}
    due = facts(3, status="handover_submitted", acks=3, handover=True, items=[item("s1", "received"), item("s2", "missing"), item("s3", "discrepancy")])
    assert day_model.driver_step(due) == {"step": 3, "label_key": "invoices_due", "sub": {"received": 1, "total_invoices": 3, "missing": 1, "discrepancy": 1, "problems": 0}}
    closed = facts(2, status="closed", acks=2, handover=True, items=[item("s1", "received"), item("s2", "received")])
    assert day_model.driver_step(closed) == {"step": "closed", "label_key": "closed", "sub": {"received": 2, "total_invoices": 2, "problems": 0}}
    assert [day_model.tile_of(day_model.driver_step(f)) for f in (facts(1), on, due, legacy, closed)] == ["loading", "on_road", "back", "back", "closed"]


def test_attention_flags_each_code():
    at = NOW - timedelta(minutes=90)
    clean = facts(2, status="on_trip", acks=2, started=NOW - timedelta(minutes=5), stops=[stop("s1", 1, "checked_out", "Delivered", at, at, distance=40.0), stop("s2", 2)])
    assert day_model.attention_flags(clean, NOW, 15, events=[ev("trip_started", 5)]) == []

    # stale: the last CREW action is 60 min old; a newer dispatcher unassign and a newer officer action do not count as activity
    stale_events = [ev("checked_in", 60, so_id="s1"), ev("order_unassigned", 1, role="dispatcher", so_id="s9", result={"previous_state": "assigned"}, id_=2), ev("invoice_validated", 2, role="logistics_officer", id_=3)]
    assert day_model.last_crew_activity(stale_events) == NOW - timedelta(minutes=60)
    flags = day_model.attention_flags(clean, NOW, 15, events=stale_events)
    assert [f["code"] for f in flags] == ["no_activity"] and flags[0]["label"] == "No activity" and flags[0]["minutes"] == 60
    assert day_model.attention_flags(clean, NOW, 90, events=stale_events) == []
    assert [f["code"] for f in day_model.attention_flags(clean, NOW, 15, events=[])] == []  # started 5 min ago, no events: fresh
    assert day_model.attention_flags(facts(2, acks=1), NOW, 15, events=[ev("acknowledged", 600)]) == []  # only on the road can be stale

    busy = facts(5, status="handover_submitted", acks=5, handover=True,
                 stops=[stop("s1", 1, "checked_out", "Failed / Returned", at, at), stop("s2", 2, "checked_out", "Partially delivered", at, at, far=True),
                        stop("s3", 3, "checked_out", "Delivered", at, at, distance=650.0), stop("s4", 4, "checked_out", "Delivered", at, at, gps=False, answers=[{"key": "outcome", "value": "Delivered"}]),
                        stop("s5", 5, "checked_out", "Delivered", at, at, answers=[{"key": "outcome", "value": "Delivered"}, {"key": "delivery_photo", "value": 7}])],
                 items=[item("s1", "missing"), item("s2", "discrepancy"), item("s3", "received"), item("s4"), item("s5")])
    events = [ev("checked_out", 100, so_id="s3", gps=False, id_=11), ev("order_unassigned", 50, role="dispatcher", so_id="s6", result={"previous_state": "acknowledged"}, id_=12),
              ev("order_unassigned", 40, role="dispatcher", so_id="s7", result={"previous_state": "assigned"}, id_=13)]
    issues = [SimpleNamespace(id=5, so_id="s2", batch_id="X", status="submitted")]
    rules = {2: [{"key": "delivery_photo", "item_type": "photo", "required": True, "show_when": {"key": "outcome", "in": ["Delivered", "Partially delivered"]}}]}
    flags = day_model.attention_flags(busy, NOW, 15, events=events, open_issues=issues, photo_rules=rules)
    by_code = {f["code"]: f for f in flags}
    assert list(by_code) == ["not_delivered", "partial", "problem_open", "far_checkin", "no_gps", "invoice_missing", "invoice_discrepancy", "order_removed", "photo_missing"]
    assert by_code["not_delivered"]["stop_so_id"] == "s1" and by_code["partial"]["stop_so_id"] == "s2" and by_code["problem_open"]["stop_so_id"] == "s2"
    assert by_code["far_checkin"]["stop_so_id"] == "s2" and by_code["no_gps"] == {"code": "no_gps", "label": "No GPS", "stop_so_id": "s3", "event_id": 11}
    assert by_code["invoice_missing"]["stop_so_id"] == "s1" and by_code["invoice_discrepancy"]["label"] == "Invoice wrong"
    assert by_code["order_removed"] == {"code": "order_removed", "label": "Order removed", "stop_so_id": "s6", "event_id": 12}  # s7 was only assigned: not flagged
    hits = day_model.attention_hits(busy, NOW, 15, events=events, open_issues=issues, photo_rules=rules)
    per_stop = day_model.flags_by_stop(hits)
    assert per_stop["s3"] == ["far_checkin", "no_gps", "photo_missing"] and per_stop["s4"] == ["no_gps", "photo_missing"] and "s5" not in per_stop  # s5 has its photo
    assert "photo_missing" not in {f["code"] for f in day_model.attention_flags(busy, NOW, 15, events=events, photo_rules=None)}
    assert set(day_model.ATTENTION_LABELS) == {"no_activity", "not_delivered", "partial", "problem_open", "far_checkin", "no_gps", "invoice_missing", "invoice_discrepancy", "order_removed", "photo_missing"}


def test_search_key_and_photo_metadata():
    assert day_model.search_key(" SO-00 123 ") == "so00123" and day_model.matches("so00123", ["x", "SO-00123"]) and not day_model.matches("so1", ["SO-2"])
    s = stop("s1", 1, "checked_out", "Partially delivered", NOW, NOW, answers=[{"key": "delivery_photo", "type": "photo", "value": 12}, {"key": "signature", "value": "13"}, {"key": "received_by", "value": "Ana"}])
    assert day_model.stop_photos(s, "SO-1") == [{"photo_id": 12, "so_number": "SO-1", "captured_at": NOW.isoformat(), "kind": "partial", "field_key": "delivery_photo"},
                                                 {"photo_id": 13, "so_number": "SO-1", "captured_at": NOW.isoformat(), "kind": "other", "field_key": "signature"}]
    issue = SimpleNamespace(photo_ids=[4], so_number="SO-1", client_ts=NOW, created_at=None)
    assert day_model.issue_photos(issue) == [{"photo_id": 4, "so_number": "SO-1", "captured_at": NOW.isoformat(), "kind": "problem", "field_key": "issue_photo"}]


# ---- endpoints ---------------------------------------------------------------------------------------------------------------


def get(env, path, who="dispatcher", status=200):
    response = env.client.get(f"/api/driver-overview{path}", headers=env.h[who])
    assert response.status_code == status, response.text
    return response.json() if status == 200 and not path.startswith("/export") else response


def add_issue(env, *, batch, so_id=None, so_number=None, vehicle=None, reporter=9, status="submitted", photos=(), created=None, description="Customer not around"):
    with env.Session() as db:
        created = created or datetime.now(timezone.utc)
        issue = DriverIssue(reporter_user_id=env.driver_user, reporter_staff_id=reporter, reporter_role="driver", batch_id=batch, so_id=so_id, so_number=so_number, vehicle=vehicle, category="Customer unavailable",
                            priority="normal", description=description, photo_ids=list(photos) or None, status=status, client_ts=created, idempotency_key=f"key-{batch}-{so_id}-{status}-{created.timestamp()}")
        db.add(issue)
        db.flush()
        issue.reference = f"IS-TEST-{issue.id:04d}"
        issue.created_at = created
        db.commit()
        return issue.id


def test_trips_rows_tiles_orders_line_and_not_assigned(env):
    day, _ = seed(env)
    body = get(env, "/trips")
    assert body["from"] == body["to"] == day.isoformat() and body["generated_at"] and body["stale_minutes"] == 15
    assert body["tile_counts"] == {"loading": 1, "on_road": 1, "back": 1, "closed": 0, "attention": 2}
    assert body["orders_line"] == {"orders": 8, "delivered": 2, "partial": 1, "failed": 1, "to_go": 4}
    assert [r["batch_id"] for r in body["trips"]] == ["A", "B", "C"] and body["total"] == 3  # attention first (A, B by plate), then loading
    assert body["not_assigned"] == [{"staff_id": 12, "name": "Glacier Idle", "warehouse": "GLACIER", "phone": "09170000012"}]
    a = body["trips"][0]
    old_keys = {"batch_id", "date", "driver_id", "driver_name", "helpers", "vehicle", "trip_number", "trips_on_date", "state", "state_label", "so_count", "kg", "kg_complete", "kg_unknown_lines", "packs",
                "acknowledged", "stops_done", "delivered", "partial", "failed", "started_at", "ended_at", "trip_minutes", "handover_status"}
    assert old_keys <= set(a)
    assert a["date"] == day.isoformat() and a["vehicle"] == "NAA1234" and a["warehouse"] == "GLACIER" and (a["trip_number"], a["trips_on_date"]) == (1, 1)
    assert a["step"]["step"] == 2 and a["step"]["label_key"] == "on_road" and a["step"]["sub"]["current"] == 2 and a["step"]["sub"]["current_so"] == "SO-A2" and a["tile"] == "on_road"
    assert a["crew"] == [{"id": 9, "name": "Emert Hirondo", "role": "driver", "phone": "09482834802"}, {"id": 17, "name": "Raymond Roblo", "role": "helper", "phone": "09170000017"}]
    assert (a["so_count"], a["acknowledged"], a["stops_done"]) == (3, 3, 1) and a["outcomes"] == {"delivered": 1, "partial": 0, "failed": 0}
    assert a["invoices"] == {"total": 3, "received": 0, "missing": 0, "discrepancy": 0, "with_driver": 1, "with_officer": 0}
    assert [f["code"] for f in a["attention"]] == ["no_activity", "no_gps"] and a["attention"][1]["stop_so_id"] == "a2"
    assert a["last_crew_activity_at"] and a["started_at"] and a["ended_at"] is None and a["duration_minutes"] is None
    assert a["so_numbers"] == ["SO-A1", "SO-A2", "SO-A3"] and a["customers"] == ["Alpha 1", "Alpha 2", "Alpha 3"] and a["invoice_nos"] == []
    b = body["trips"][1]
    assert b["tile"] == "back" and b["step"]["label_key"] == "invoices_due" and b["invoices"]["with_officer"] == 3 and [f["code"] for f in b["attention"]] == ["not_delivered", "partial"]
    assert b["duration_minutes"] == 420.0 and b["warehouse"] == "METS"
    c = body["trips"][2]
    assert c["tile"] == "loading" and c["step"]["label_key"] == "waiting_crew" and c["attention"] == [] and c["last_crew_activity_at"] is None


def test_tile_counts_equal_rows_for_every_filter(env):
    day, yesterday = seed(env)
    for query in ("", "&warehouse=METS", "&warehouse=GLACIER", "&q=alpha", "&q=jaymar", f"&from={yesterday}&to={day}&date=", f"&from={yesterday}&to={day}&q=emert"):
        base = get(env, f"/trips?{query.lstrip('&')}")
        assert sum(base["tile_counts"][t] for t in ("loading", "on_road", "back", "closed")) == len(base["trips"]) == base["total"], query
        for tile in ("loading", "on_road", "back", "closed", "attention"):
            shown = get(env, f"/trips?tile={tile}{query}")
            assert shown["total"] == len(shown["trips"]) == base["tile_counts"][tile], (query, tile)
            assert shown["tile_counts"] == base["tile_counts"] and shown["orders_line"] == base["orders_line"]  # tile never changes the counts
            assert all((bool(r["attention"]) if tile == "attention" else r["tile"] == tile) for r in shown["trips"])
    assert env.client.get("/api/driver-overview/trips?tile=stale", headers=env.h["admin"]).status_code == 422


def test_two_batches_on_one_truck_are_two_rows_and_helpers_never_rows(env):
    day, _ = seed(env)
    for i in (1, 2):
        t.add_so(env, f"d{i}", number=f"SO-D{i}", customer=f"Delta {i}", batch="D", driver=9, helpers=(17,), day=day, offset=90 + i)  # second Assign click, same truck NAA1234
    rows = get(env, "/trips")["trips"]
    mine = sorted((r["trip_number"], r["trips_on_date"], r["batch_id"], r["vehicle"]) for r in rows if r["driver_id"] == 9)
    assert mine == [(1, 2, "A", "NAA1234"), (2, 2, "D", "NAA1234")]
    assert len(rows) == 4 and {r["driver_id"] for r in rows} == {9, 10, 11}  # helpers 17 / 18 ride along, never their own row
    assert all(r["crew"][0]["role"] == "driver" and all(c["role"] == "helper" for c in r["crew"][1:]) for r in rows)


def test_search_matches_driver_helper_plate_so_to_invoice_customer(env):
    day, yesterday = seed(env)
    t.add_so(env, "TO:42", number="TO-00042", customer="Glacier Warehouse", batch="T", driver=11, day=day, vehicle="NTT4242", offset=60)
    with env.Session() as db:
        db.execute(InvoiceHandoverItem.__table__.update().where(InvoiceHandoverItem.so_id == "b1").values(invoice_no="000777"))
        db.commit()
    overview.reset_cache()
    found = lambda q: sorted(r["batch_id"] for r in get(env, f"/trips?q={q}")["trips"])  # noqa: E731
    assert found("emert") == ["A"] and found("EMERT%20hirondo") == ["A"]  # driver (case / space insensitive)
    assert found("roblo") == ["A"] and found("jaymar") == ["B", "C"]  # helpers
    assert found("nbb-5678") == ["B"] and found("nbb5678") == ["B"]  # plate, punctuation-insensitive
    assert found("so-c1") == ["C"] and found("soc1") == ["C"] and found("to42") == ["T"] and found("TO-00042") == ["T"]  # SO / TO numbers
    assert found("000777") == ["B"] and found("777") == ["B"]  # invoice / DR number
    assert found("gamma%202") == ["C"] and found("beta") == ["B"]  # customer
    assert found("nobody") == [] and found("%20-%20") == ["A", "B", "C", "T"]  # punctuation only = no filter
    assert sorted(r["batch_id"] for r in get(env, f"/trips?from={yesterday}&to={day}&q=emert")["trips"]) == ["A", "E"]
    assert [s["staff_id"] for s in get(env, "/trips?q=glacier%20idle")["not_assigned"]] == [12] and get(env, "/trips?q=emert")["not_assigned"] == []


def test_range_limit_and_range_rows(env):
    day, yesterday = seed(env)
    assert env.client.get("/api/driver-overview/trips?from=2025-01-01&to=2026-01-02", headers=env.h["admin"]).status_code == 422  # 367 days
    assert env.client.get("/api/driver-overview/trips?from=2025-01-02&to=2026-01-02", headers=env.h["admin"]).status_code == 200  # 366 days
    rows = get(env, f"/trips?from={yesterday}&to={day}")["trips"]
    assert [r["date"] for r in rows] == [day.isoformat()] * 3 + [yesterday.isoformat()] and rows[-1]["batch_id"] == "E"  # newest date first (grouped by date)
    assert rows[-1]["tile"] == "back" and [f["code"] for f in rows[-1]["attention"]] == ["invoice_missing"]


def test_warehouse_filter_applies_to_trips_hardcopies_issues_and_export(env):
    seed(env)
    add_issue(env, batch="A", so_id="a2", so_number="SO-A2", vehicle="NAA1234")
    add_issue(env, batch="B", so_id="b1", so_number="SO-B1", vehicle="NBB5678", reporter=18)
    assert [r["batch_id"] for r in get(env, "/trips?warehouse=METS")["trips"]] == ["B", "C"]
    assert get(env, "/trips?warehouse=METS")["not_assigned"] == [] and [s["staff_id"] for s in get(env, "/trips?warehouse=GLACIER")["not_assigned"]] == [12]
    copies = get(env, "/hardcopies?warehouse=METS")
    assert sorted(r["so_number"] for r in copies["rows"]) == ["SO-B1", "SO-B2", "SO-B3"] and copies["counts"] == {"with_officer": 3}
    glacier = get(env, "/hardcopies?warehouse=GLACIER")["rows"]
    assert sorted(r["so_number"] for r in glacier) == ["SO-A1", "SO-E1"]  # A1: checked out, copy with the driver; E1: missing
    assert [i["batch_id"] for i in get(env, "/issues?warehouse=METS")["issues"]] == ["B"] and [i["batch_id"] for i in get(env, "/issues?warehouse=GLACIER")["issues"]] == ["A"]
    text = get(env, "/export.csv?warehouse=METS").content.decode("utf-8-sig")
    assert sorted(r[5] for r in list(csv.reader(io.StringIO(text)))[1:]) == ["SO-B1", "SO-B2", "SO-B3", "SO-C1", "SO-C2"]


def test_export_with_q_and_tile_keeps_columns_and_encoding(env):
    seed(env)
    full = get(env, "/export.csv")
    header = list(csv.reader(io.StringIO(full.content.decode("utf-8-sig"))))[0]
    for query, expected in (("q=so-b2", ["SO-B1", "SO-B2", "SO-B3"]), ("tile=loading", ["SO-C1", "SO-C2"]), ("tile=attention", ["SO-A1", "SO-A2", "SO-A3", "SO-B1", "SO-B2", "SO-B3"]),
                            ("q=alpha&tile=back", []), ("q=gamma&tile=loading", ["SO-C1", "SO-C2"])):
        response = get(env, f"/export.csv?{query}")
        assert response.content.startswith(b"\xef\xbb\xbf") and response.headers["content-type"].startswith("text/csv") and "attachment" in response.headers["content-disposition"]
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert rows[0] == header and all(len(r) == len(header) for r in rows[1:]), query
        assert sorted(r[5] for r in rows[1:]) == expected, query
        trips = get(env, f"/trips?{query}")["trips"]
        assert sorted(so for r in trips for so in r["so_numbers"]) == expected  # the file equals the screen
    assert env.client.get("/api/driver-overview/export.csv?tile=nope", headers=env.h["admin"]).status_code == 422


def test_trip_drawer_additions(env):
    day, yesterday = seed(env)
    add_issue(env, batch="A", so_id="a2", so_number="SO-A2", vehicle="NAA1234", photos=(31, 32))
    add_issue(env, batch="A", status="resolved", description="Old problem fixed")
    tl = get(env, "/trips/A")
    assert {"batch", "trip", "crew", "orders", "handover", "events"} <= set(tl)  # every existing key kept
    assert tl["step"]["step"] == 2 and tl["tile"] == "on_road" and [c["role"] for c in tl["crew"]] == ["driver", "helper"] and tl["crew"][0]["phone"] == "09482834802"
    assert [a["code"] for a in tl["attention"]] == ["no_activity", "problem_open", "no_gps"]
    assert (tl["batch"]["trip_number"], tl["batch"]["trips_on_date"], tl["batch"]["warehouse"], tl["batch"]["kg_complete"]) == (1, 1, "GLACIER", False)
    by_so = {o["so_number"]: o for o in tl["orders"]}
    assert by_so["SO-A1"]["flags"] == [] and by_so["SO-A2"]["flags"] == ["problem_open", "no_gps"] and by_so["SO-A3"]["flags"] == []
    a1_checkout = by_so["SO-A1"]["stop"]["checkout"]["at"]
    assert by_so["SO-A1"]["photos"] == [{"photo_id": 12, "so_number": "SO-A1", "captured_at": a1_checkout, "kind": "delivered", "field_key": "delivery_photo"}]
    assert [(p["photo_id"], p["kind"], p["so_number"]) for p in by_so["SO-A2"]["photos"]] == [(31, "problem", "SO-A2"), (32, "problem", "SO-A2")] and by_so["SO-A3"]["photos"] == []
    assert tl["invoices"] == [{"so_id": "a1", "so_number": "SO-A1", "invoice_no": None, "status": "with_driver", "officer_name": None, "checked_at": None, "officer_note": None}]
    assert [i["status"] for i in tl["issues"]] == ["submitted", "resolved"] and tl["issues"][0]["photos"][0]["photo_id"] == 31 and tl["issues"][0]["batch_id"] == "A"
    issue_keys = set(get(env, "/issues?status=all")["issues"][0])
    assert set(tl["issues"][0]) == issue_keys  # exactly like /issues items
    assert "other_days" not in tl
    b = get(env, "/trips/B")
    assert [i["status"] for i in b["invoices"]] == ["with_officer"] * 3 and b["tile"] == "back" and "other_days" not in b
    with env.Session() as db:
        db.execute(InvoiceHandoverItem.__table__.update().where(InvoiceHandoverItem.so_id == "b2").values(officer_status="discrepancy", officer_note="total differs", officer_user_id=env.officer, officer_checked_at=datetime.now(timezone.utc)))
        db.commit()
    overview.reset_cache()
    b = get(env, "/trips/B")
    wrong = next(i for i in b["invoices"] if i["so_id"] == "b2")
    assert wrong["status"] == "discrepancy" and wrong["officer_name"] == "Olivia Officer" and wrong["officer_note"] == "total differs" and wrong["checked_at"]
    assert "invoice_discrepancy" in {a["code"] for a in b["attention"]} and "invoice_discrepancy" in {o["so_id"]: o for o in b["orders"]}["b2"]["flags"]
    c = get(env, "/trips/C")
    assert c["step"]["label_key"] == "waiting_crew" and c["tile"] == "loading" and c["invoices"] == [] and c["issues"] == [] and c["attention"] == []
    assert env.client.get("/api/driver-overview/trips/nope", headers=env.h["admin"]).status_code == 404


def test_photo_missing_when_a_required_photo_was_not_stored(env):
    seed(env)
    with env.Session() as db:
        db.execute(FormTemplateItem.__table__.update().where(FormTemplateItem.template_id == 2, FormTemplateItem.key == "delivery_photo").values(required=True))
        db.commit()
    overview.reset_cache()
    rows = {r["batch_id"]: r for r in get(env, "/trips")["trips"]}
    assert "photo_missing" in {f["code"] for f in rows["B"]["attention"]} and "photo_missing" not in {f["code"] for f in rows["A"]["attention"]}  # A1 has its photo
    flags = {o["so_id"]: o["flags"] for o in get(env, "/trips/B")["orders"]}
    assert "photo_missing" in flags["b1"] and "photo_missing" in flags["b2"] and "photo_missing" not in flags["b3"]  # b3 failed: no proof photo asked


def test_order_removed_and_unassign_not_activity(env):
    day, _ = seed(env)
    now = datetime.now(timezone.utc)
    with env.Session() as db:
        trip_id = db.execute(select(TripEvent.trip_id).where(TripEvent.batch_id == "A")).scalars().first()
        db.add(TripEvent(trip_id=trip_id, batch_id="A", event_type="order_unassigned", so_id="a9", user_id=env.dispatcher, role="dispatcher", client_ts=now, received_at=now,
                         result={"so_number": "SO-A9", "previous_state": "trip_started", "by_name": "Dee Dispatcher"}, payload={"driver_id": 9}))
        db.commit()
    overview.reset_cache()
    row = next(r for r in get(env, "/trips")["trips"] if r["batch_id"] == "A")
    codes = [f["code"] for f in row["attention"]]
    assert codes[0] == "no_activity" and "order_removed" in codes  # the unassign is newer, yet the crew is still "no activity"
    assert row["last_crew_activity_at"] < now.isoformat()
    tl = get(env, "/trips/A")
    assert any(e["type"] == "order_unassigned" for e in tl["events"]) and next(a for a in tl["attention"] if a["code"] == "order_removed")["stop_so_id"] == "a9"


def test_issues_and_hardcopies_filters(env):
    day, yesterday = seed(env)
    old = datetime.now(timezone.utc) - timedelta(days=3)
    add_issue(env, batch="A", so_id="a2", so_number="SO-A2", vehicle="NAA1234", photos=(31,))
    add_issue(env, batch="B", so_id="b1", so_number="SO-B1", vehicle="NBB5678", reporter=18, created=old, description="Truck tyre flat")
    items = get(env, "/issues")["issues"]
    assert [(i["batch_id"], i["vehicle"]) for i in items] == [("A", "NAA1234"), ("B", "NBB5678")]
    assert items[0]["photos"] == [{"photo_id": 31, "so_number": "SO-A2", "captured_at": items[0]["photos"][0]["captured_at"], "kind": "problem", "field_key": "issue_photo"}] and items[0]["photos"][0]["captured_at"]
    assert items[0]["photo_ids"] == [31] and "categories" in get(env, "/issues") and "statuses" in get(env, "/issues")
    assert [i["batch_id"] for i in get(env, "/issues?q=tyre")["issues"]] == ["B"] and [i["batch_id"] for i in get(env, "/issues?q=nbb%205678")["issues"]] == ["B"]
    assert [i["batch_id"] for i in get(env, "/issues?q=jaymar")["issues"]] == ["B"]  # reporter name
    assert [i["batch_id"] for i in get(env, f"/issues?date={day}")["issues"]] == ["A"] and get(env, f"/issues?date={yesterday}")["issues"] == []
    assert env.client.get("/api/driver-overview/issues?date=nope", headers=env.h["admin"]).status_code == 422
    assert env.client.get("/api/driver-overview/issues?status=weird", headers=env.h["admin"]).status_code == 422
    copies = get(env, "/hardcopies")
    assert copies["total"] == 5 and {r["so_number"]: r["state"] for r in copies["rows"]} == {"SO-A1": "with_driver", "SO-B1": "with_officer", "SO-B2": "with_officer", "SO-B3": "with_officer", "SO-E1": "missing"}
    assert all({"so_id", "vehicle", "driver_name", "helpers"} <= set(r) for r in copies["rows"])
    assert [r["so_number"] for r in get(env, "/hardcopies?q=epsilon")["rows"]] == ["SO-E1"] and [r["so_number"] for r in get(env, "/hardcopies?q=roblo")["rows"]] == ["SO-A1"]
    assert sorted(r["so_number"] for r in get(env, f"/hardcopies?date={day}")["rows"]) == ["SO-A1", "SO-B1", "SO-B2", "SO-B3"]
    only_yesterday = get(env, f"/hardcopies?date={yesterday}")
    assert [r["so_number"] for r in only_yesterday["rows"]] == ["SO-E1"] and only_yesterday["counts"] == {"missing": 1}


def test_legacy_endpoints_still_work_and_acknowledge_label(env):
    seed(env)
    for path in ("/summary", "/board", "/drivers/9", "/map", "/hardcopies", "/issues", "/export.csv", "/trips?driver_id=10"):
        assert env.client.get(f"/api/driver-overview{path}", headers=env.h["dispatcher"]).status_code == 200, path
    assert [r["batch_id"] for r in get(env, "/trips?driver_id=10")["trips"]] == ["B"]
    assert overview.EVENT_LABELS["acknowledged"].format(so="SO-1") == "Loaded (acknowledged) SO-1"


def test_v2_reads_are_cached_per_generation(env):
    seed(env)
    reads: list[str] = []
    # Authentication must still validate each caller against users; only the
    # overview business reads are shared by the endpoint cache.
    event.listen(env.engine, "before_cursor_execute", lambda conn, cur, stmt, *a: reads.append(stmt) if stmt.lstrip().upper().startswith("SELECT") and "FROM users" not in stmt else None)
    for path in ("/trips", "/trips/A", "/issues", "/hardcopies?q=alpha"):
        get(env, path)
        first = len(reads)
        get(env, path, "admin")
        assert len(reads) == first, path  # second call within 15 s and the same generation: memory
    trip_events.bump()
    before = len(reads)
    get(env, "/trips")
    assert len(reads) > before
