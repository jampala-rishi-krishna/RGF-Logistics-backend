"""Fleet Health phases B/C: the pure calculators (service status, battery, risk, eco score, km/L, CO2)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from services.fleet_health import config, eco, eco_views, risk

TODAY = date(2026, 10, 8)
MANILA = config.MANILA


# ---- service status -----------------------------------------------------------------------------------------
INTERVAL = {"interval_km": 10000, "interval_engine_hours": 250, "interval_days": 180, "active": True, "confirmed": False}


def status(**kw):
    base = dict(interval=INTERVAL, last_record={"performed_on": TODAY - timedelta(days=10), "odometer_km": 50000.0}, odometer_km=50500.0, engine_hours_since=5.0, today=TODAY)
    return risk.service_status(**{**base, **kw})


def test_usage_bands_ok_due_soon_overdue():
    assert status()["status"] == "ok"
    due = status(odometer_km=50000 + 8500)            # 85% of 10,000 km
    assert due["status"] == "due_soon" and due["usage_pct"] == 85.0 and due["driven_by"] == "km"
    over = status(odometer_km=50000 + 10100)          # 101%
    assert over["status"] == "overdue"
    assert status(odometer_km=50000 + 10000)["status"] == "due_soon"      # exactly 100% is not yet overdue ("overdue > 100%")
    assert status(odometer_km=50000 + 7999)["status"] == "ok"             # 79.99% is still OK; 80% starts "due soon"
    assert status(odometer_km=50000 + 8000)["status"] == "due_soon"


def test_usage_is_the_worst_of_km_hours_and_days():
    by_days = status(last_record={"performed_on": TODAY - timedelta(days=200), "odometer_km": 50400.0})
    assert by_days["status"] == "overdue" and by_days["driven_by"] == "days"
    by_hours = status(engine_hours_since=300)
    assert by_hours["status"] == "overdue" and by_hours["driven_by"] == "engine_hours"
    assert set(by_hours["components"]) == {"km", "engine_hours", "days"}


def test_no_record_never_guesses_and_missing_data_is_explicit():
    assert status(last_record=None) == {"status": "no_record", "usage_pct": None, "components": {}, "interval": {"km": 10000, "engine_hours": 250, "days": 180, "confirmed": False, "scope": "fleet"}}
    assert status(interval=None)["status"] == "not_tracked"
    no_data = status(interval={**INTERVAL, "interval_days": None, "interval_engine_hours": None}, odometer_km=None)
    assert no_data["status"] == "no_record" and "missing odometer" in no_data["note"]


def test_truck_override_beats_fleet_default_and_inactive_is_ignored():
    default = {"oil_change": {"interval_km": 10000, "active": True}}
    override = {"oil_change": {"interval_km": 5000, "active": True, "vehicle_id": 4}}
    assert risk.effective_interval("oil_change", override, default)["interval_km"] == 5000
    assert risk.effective_interval("oil_change", {}, default)["interval_km"] == 10000
    assert risk.effective_interval("oil_change", {"oil_change": {"active": False}}, {}) is None
    assert risk.effective_interval("tires", {}, default) is None


# ---- battery -------------------------------------------------------------------------------------------------
def b(parked=None, running=None, system=12):
    return {"vext_parked_min": parked, "vext_running_avg": running, "electrical_system": system}


def test_battery_thresholds_12v_and_24v():
    assert risk.battery_day_status(b(12.6, 14.1))["status"] == "ok"
    assert risk.battery_day_status(b(12.1, 14.1))["status"] == "warning"          # < 12.2
    assert risk.battery_day_status(b(11.8, 14.1))["status"] == "critical"         # < 11.9
    assert risk.battery_day_status(b(12.6, 13.0))["status"] == "warning"          # charging below 13.5 V: check alternator
    assert "alternator" in risk.battery_day_status(b(12.6, 15.2))["reasons"][0]
    assert risk.battery_day_status(b(25.0, 28.0, 24))["status"] == "ok"
    assert risk.battery_day_status(b(24.3, 28.0, 24))["status"] == "warning"      # < 24.4
    assert risk.battery_day_status(b(23.7, 28.0, 24))["status"] == "critical"     # < 23.8
    assert risk.battery_day_status(b(25.0, 26.0, 24))["status"] == "warning"      # outside 27-29
    assert risk.battery_day_status(b())["status"] == "no_data"


def day(offset, parked, running=14.0, system=12):
    return {"stat_date": TODAY - timedelta(days=offset), **b(parked, running, system)}


def test_battery_flag_after_two_consecutive_warning_days_or_one_critical():
    one_warning = risk.consecutive_tail([day(1, 12.6), day(0, 12.1)])
    assert risk.battery_flag_decision(one_warning) is None
    two = risk.consecutive_tail([day(2, 12.6), day(1, 12.1), day(0, 12.0)])
    assert risk.battery_flag_decision(two)["severity"] == "warning"
    critical = risk.consecutive_tail([day(1, 12.6), day(0, 11.5)])
    assert risk.battery_flag_decision(critical)["severity"] == "critical"
    gap = risk.consecutive_tail([day(3, 12.0), day(1, 12.0), day(0, 12.6)])        # a missing day and a good day break the streak
    assert risk.battery_flag_decision(gap) is None
    no_data_gap = risk.consecutive_tail([day(2, 12.0), {"stat_date": TODAY - timedelta(days=1), **b()}, day(0, 12.0)])
    assert risk.battery_flag_decision(no_data_gap) is None


# ---- risk score ------------------------------------------------------------------------------------------------
def flag(sev):
    return {"severity": sev}


def test_risk_score_weights_caps_bands_and_breakdown():
    ok = {"oil_change": {"status": "ok", "usage_pct": 20}}
    r = risk.risk_score(service_statuses=ok, open_flags=[], failed_checklists_7d=0, overloads_30d=0, repairs_90d=0)
    assert (r["score"], r["band"], r["needs_attention"]) == (0, "low", False)
    assert [p["key"] for p in r["breakdown"]] == ["service", "flags", "checklists", "overloads", "repairs"]
    due = risk.risk_score(service_statuses={"x": {"status": "due_soon", "usage_pct": 90}}, open_flags=[], failed_checklists_7d=0, overloads_30d=0, repairs_90d=0)
    assert due["score"] == 20
    overdue = risk.risk_score(service_statuses={"x": {"status": "overdue", "usage_pct": 130}}, open_flags=[], failed_checklists_7d=0, overloads_30d=0, repairs_90d=0)
    assert overdue["score"] == 40 and overdue["needs_attention"] is True and overdue["band"] == "medium"
    flags = risk.risk_score(service_statuses=ok, open_flags=[flag("critical")] * 3 + [flag("warning")] * 4, failed_checklists_7d=0, overloads_30d=0, repairs_90d=0)
    assert flags["score"] == 25                              # 3x15 + 4x5 = 65, capped at 25
    assert risk.risk_score(service_statuses=ok, open_flags=[flag("info")], failed_checklists_7d=0, overloads_30d=0, repairs_90d=0)["score"] == 0
    caps = risk.risk_score(service_statuses=ok, open_flags=[], failed_checklists_7d=9, overloads_30d=40, repairs_90d=9)
    assert [p["points"] for p in caps["breakdown"]] == [0, 0, 15, 10, 10] and caps["score"] == 35 and caps["band"] == "medium"


def test_high_band_starts_at_60_and_needs_attention():
    r = risk.risk_score(service_statuses={"x": {"status": "overdue", "usage_pct": 200}}, open_flags=[flag("critical")] * 2, failed_checklists_7d=2, overloads_30d=0, repairs_90d=0)
    assert r["score"] == 40 + 25 + 10 and r["band"] == "high" and r["needs_attention"] is True


# ---- fuel: full-to-full km/L, CO2, checks -----------------------------------------------------------------------
def fill(day_offset, litres, odo, full=True, id=None):
    return {"id": id, "filled_at": datetime(2026, 9, 1, 8, tzinfo=MANILA) + timedelta(days=day_offset), "litres": litres, "odometer_km": odo, "full_tank": full}


def test_full_to_full_uses_litres_added_since_the_previous_full_fill():
    fills = [fill(0, 50, 1000), fill(3, 20, 1300, full=False), fill(5, 30, 1500)]          # 500 km, 20 + 30 litres
    [interval] = eco.full_to_full(fills)
    assert (interval["km"], interval["litres"], interval["kmpl"]) == (500.0, 50.0, 10.0)
    assert eco.full_to_full([fill(0, 50, 1000), fill(5, 30, 1500, full=False)]) == []        # no second FULL fill, no interval
    assert eco.full_to_full([fill(0, 50, None), fill(5, 30, 1500)]) == []                    # missing odometer: never guessed
    two = eco.full_to_full([fill(0, 50, 1000), fill(5, 40, 1400), fill(9, 40, 1900)])
    assert [i["kmpl"] for i in two] == [10.0, 12.5]


def test_the_analog_sensor_is_never_a_kmpl_source():
    import inspect
    assert "fuel_pct" not in inspect.getsource(eco.full_to_full)


def test_co2_is_litres_times_2_68():
    assert eco.co2_kg(100) == 268.0 and eco.co2_kg(None) is None and eco.co2_kg(0) == 0.0


def test_fuel_log_checks_flag_overfill_and_implausible_kmpl():
    checks = eco.fuel_log_checks([fill(0, 120, 1000, id=1)], capacity_l=100)
    assert "more than the 100 L tank" in checks[0]["reason"] and checks[0]["reason"].endswith("check")
    unconfirmed = eco.fuel_log_checks([fill(0, 300, 1000, id=1)], capacity_l=200, capacity_unconfirmed=True)
    assert "tank capacity unconfirmed" in unconfirmed[0]["reason"]
    weird = eco.fuel_log_checks([fill(0, 50, 1000), fill(2, 60, 1030)], capacity_l=100)   # 30 km on 60 L = 0.5 km/L
    assert any("looks unusual" in c["reason"] for c in weird)
    assert eco.fuel_log_checks([fill(0, 50, 1000), fill(5, 50, 1500)], capacity_l=100) == []


# ---- eco score ---------------------------------------------------------------------------------------------------
def drow(km=100.0, engine=36000, idle_total=3600, at_stop=0, elsewhere=3600, speeding=0, harsh=(0, 0, 0), assigned=True, staff=13, vehicle=4, offset=0):
    return {"vehicle_id": vehicle, "stat_date": date(2026, 9, 28) + timedelta(days=offset), "km_driven": km, "engine_seconds": engine, "idle_seconds_total": idle_total,
            "idle_seconds_at_stop": at_stop if assigned else None, "idle_seconds_elsewhere": elsewhere if assigned else None, "speeding_events": 0, "speeding_seconds": speeding,
            "max_speed_kmh": 80, "harsh_braking": harsh[0], "harsh_acceleration": harsh[1], "harsh_cornering": harsh[2], "assigned": assigned, "primary_staff_id": staff if assigned else None}


def test_under_50_km_is_not_enough_data():
    result = eco.driver_eco_score([drow(km=30), drow(km=19.9, offset=1)])
    assert result["score"] is None and result["status"] == "not_enough_data" and "50 km" in result["reason"]
    assert eco.driver_eco_score([drow(km=50)])["status"] == "scored"


def test_clean_driving_scores_100_and_each_penalty_has_its_cap():
    clean = eco.driver_eco_score([drow(km=200, idle_total=0, elsewhere=0)])
    assert clean["score"] == 100 and [b["penalty"] for b in clean["breakdown"]] == [0, 0, 0, 0]
    bad = eco.driver_eco_score([drow(km=100, speeding=100000, harsh=(500, 0, 0), elsewhere=10 * 3600, idle_total=10 * 3600, engine=12 * 3600)], kmpl=2.0, fleet_kmpl=10.0)
    assert [b["penalty"] for b in bad["breakdown"]] == [30, 25, 25, 20] and bad["score"] == 0


def test_penalty_formulas():
    r = eco.driver_eco_score([drow(km=200, speeding=2000, harsh=(2, 1, 1), elsewhere=1800, idle_total=1800, engine=10 * 3600)])
    penalties = {b["key"]: b["penalty"] for b in r["breakdown"]}
    # speeding: 2000 s over 200 km = 1000 s per 100 km x 0.05 = 50, capped at 30
    # harsh: 4 events over 200 km = 2 per 100 km x 2.5 = 5.0
    # idle elsewhere: 30 min over (10 h - 0.5 h) = 9.5 driving hours = 3.16 min/h x 1.0 = 3.2
    assert penalties == {"speeding": 30, "harsh": 5.0, "idle": 3.2, "kmpl": 0.0}
    assert r["score"] == 62


def test_idle_at_stops_is_shown_but_never_penalised():
    at_stops = eco.driver_eco_score([drow(km=100, idle_total=7200, at_stop=7200, elsewhere=0)])
    assert at_stops["totals"]["idle_at_stop_min"] == 120.0 and at_stops["score"] == 100
    elsewhere = eco.driver_eco_score([drow(km=100, idle_total=7200, at_stop=0, elsewhere=7200)])
    assert elsewhere["score"] < 100


def test_kmpl_term_only_when_the_truck_has_fuel_logs():
    base = eco.driver_eco_score([drow(km=100, idle_total=0, elsewhere=0)])
    assert base["score"] == 100 and "not scored" in base["breakdown"][3]["detail"]
    below = eco.driver_eco_score([drow(km=100, idle_total=0, elsewhere=0)], kmpl=8.0, fleet_kmpl=10.0)
    assert below["breakdown"][3]["penalty"] == 10.0 and below["score"] == 90          # 20% below x 0.5
    better = eco.driver_eco_score([drow(km=100, idle_total=0, elsewhere=0)], kmpl=12.0, fleet_kmpl=10.0)
    assert better["score"] == 100


def test_score_band():
    assert [eco.score_band(s) for s in (None, 95, 85, 70, 69, 50, 49)] == ["none", "great", "great", "good", "watch", "watch", "poor"]


# ---- rollups & driver attribution ----------------------------------------------------------------------------------
def test_unassigned_km_stays_in_truck_totals_but_idle_is_unclassified():
    totals = eco.rollup([drow(km=100), drow(km=40, assigned=False, offset=1, idle_total=1800)])
    assert (totals["km"], totals["assigned_km"], totals["unassigned_km"]) == (140.0, 100.0, 40.0)
    assert totals["idle_elsewhere_min"] == 60.0 and totals["unclassified_idle_min"] == 30.0


def test_driver_scores_only_count_assigned_days_and_rank_with_trend(monkeypatch):
    monkeypatch.setattr(eco_views, "staff_name", lambda sid: {13: "Juan", 7: "Pedro"}.get(sid, f"Driver #{sid}"))
    monday = date(2026, 9, 28)
    last_week = date(2026, 9, 21)
    rows = [drow(km=100, staff=13, offset=0), drow(km=100, speeding=40000, harsh=(10, 0, 0), staff=7, offset=1, vehicle=5),
            drow(km=300, assigned=False, offset=2),                                       # unassigned: nobody's score
            {**drow(km=100, staff=13), "stat_date": last_week + timedelta(days=1), "speeding_seconds": 20000}]
    board = eco_views.driver_scores(rows, [], monday, plate_of={4: "NFX5791", 5: "NAJ6018"})
    assert [d["name"] for d in board] == ["Juan", "Pedro"] and [d["rank"] for d in board] == [1, 2]
    juan = board[0]
    assert juan["totals"]["assigned_km"] == 100.0 and juan["trucks"] == ["NFX5791"]
    assert juan["previous_score"] is not None and juan["trend"] == juan["score"] - juan["previous_score"]
    assert board[1]["score"] < juan["score"]


def test_scorecard_message_picks_the_biggest_penalty_and_skips_unscored():
    driver = {"name": "Juan Dela Cruz", "score": 72, "trend": -3, "breakdown": [{"key": "speeding", "penalty": 20}, {"key": "harsh", "penalty": 5}, {"key": "idle", "penalty": 3}, {"key": "kmpl", "penalty": 0}]}
    message = eco_views.scorecard_message(driver)
    assert "Hi Juan!" in message["text"] and "72/100" in message["text"] and "-3 vs last week" in message["text"] and "bilis" in message["text"]
    assert eco_views.scorecard_message({"name": "X", "score": None, "breakdown": []}) == {"text": None, "skip_reason": "Not enough data this week"}
    perfect = eco_views.scorecard_message({"name": "Ana", "score": 100, "trend": None, "breakdown": [{"key": "speeding", "penalty": 0}]})
    assert "Ituloy" in perfect["text"]
