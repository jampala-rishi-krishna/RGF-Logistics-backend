"""Reconciliation: for one date, the numbers PER PERSON must be identical in the three places they are shown.

  1. Load Planning > Confirmed SO  (routers/load_planning._summary: its own serializer, its own weight/pack logic)
  2. the Driver App                (services/driver_assignments.build_batches, exactly what /api/driver/today returns)
  3. Driver Overview               (services/overview.board + overview.summary)

Compared per person (driver or helper): SOs, kg, packs, batches. kg/packs of "unknown" are None on every side, never 0.
Zero Zoho calls: the Load Planning side reads the in-memory assigned records when they are warm (today / future) and Neon
otherwise (past dates, same as the Confirmed SO screen); orders it cannot read are listed in `lp_missing`, never guessed.
Read-only: nothing here writes.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.sales_order_history import SalesOrderHistory
from services import day_model, driver_assignments, live_sales_order_cache, overview, staff_directory_cache
from services.staff_roles import helper_ids_of

FIELDS = ("sos", "kg", "packs", "batches")
KG_TOLERANCE = 0.01


def _name(staff_id: int) -> str:
    return (staff_directory_cache.get_by_id(staff_id) or {}).get("name") or f"Staff #{staff_id}"


def _crew(row) -> list[int]:
    return ([row.driver_id] if row.driver_id is not None else []) + list(helper_ids_of(row.driver_id, row.helper_ids))


def _blank() -> dict:
    return {"sos": 0, "kg": 0.0, "packs": None, "batches": set()}


def _add_packs(entry: dict, packs: float | None) -> None:
    if packs is not None:
        entry["packs"] = round((entry["packs"] or 0.0) + packs, 2)


def _finish(side: dict[int, dict]) -> dict[int, dict]:
    return {pid: {"sos": e["sos"], "kg": round(e["kg"], 2), "packs": e["packs"], "batches": len(e["batches"])} for pid, e in side.items()}


# ---- side 1: Load Planning ---------------------------------------------------------------------------------------------------


def load_planning_side(db: Session, day: date, source: str = "auto") -> tuple[dict[int, dict], str, list[str]]:
    """(per person, source used, order ids that could not be read)."""
    from routers import load_planning  # lazy: it is a router module

    today = driver_assignments.manila_today()
    missing: list[str] = []
    used = "neon"
    rows: list = []
    if source in ("auto", "memory") and day >= today:
        cached = [r for r in live_sales_order_cache.cached_assigned_records() if r.expected_shipment_date == day and (r.assignment_status or "unassigned") in {"assigned", "completed"}]
        expected = {oid for oid, state in _assigned_state().items() if state.get("assignment_status") in {"assigned", "completed"}}
        if cached or source == "memory":
            used, rows = "memory", cached
            missing = sorted(oid for oid in expected - {r.id for r in cached} if _expected_on(db, oid, day))
    if used == "neon":
        # What the Confirmed SO screen reads for an earlier date: the saved rows, status assigned or completed.
        rows = [r for r in db.execute(select(SalesOrderHistory).where(SalesOrderHistory.expected_shipment_date == day)).scalars().all() if (r.assignment_status or "unassigned") in {"assigned", "completed"}]
    side: dict[int, dict] = defaultdict(_blank)
    for row in rows:
        summary = load_planning._summary(row, db, allow_fetch=False)
        products = summary.get("products") or []
        known = [p["total_weight_kg"] for p in products if p.get("total_weight_kg") is not None]
        packs = sum(float(p.get("quantity") or 0) for p in products if p.get("packaging_type") == "pack") if any(p.get("packaging_type") == "pack" for p in products) else None
        batch = getattr(row, "assignment_batch_id", None) or day_model.legacy_batch_key(row)
        for person in _crew(row):
            entry = side[person]
            entry["sos"] += 1
            entry["kg"] += sum(float(w) for w in known)
            _add_packs(entry, packs)
            entry["batches"].add(batch)
    return _finish(side), used, missing


def _assigned_state() -> dict:
    with live_sales_order_cache._state_lock:
        return {oid: dict(state) for oid, state in live_sales_order_cache._assignment_state.items()}


def _expected_on(db: Session, order_id: str, day: date) -> bool:
    row = db.get(SalesOrderHistory, order_id)
    return row is not None and row.expected_shipment_date == day


# ---- side 2: Driver App ------------------------------------------------------------------------------------------------------


def driver_app_side(db: Session, day: date, person_ids: set[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for person in person_ids:
        batches = driver_assignments.build_batches(db, driver_assignments.rows_for_driver(db, person, day, day), person)
        if not batches:
            continue
        packs = [b["total_packs"] for b in batches if b["total_packs"] is not None]
        out[person] = {"sos": sum(b["so_count"] for b in batches), "kg": round(sum(b["total_kg"] for b in batches), 2), "packs": round(sum(packs), 2) if packs else None, "batches": len(batches)}
    return out


# ---- side 3: Driver Overview -------------------------------------------------------------------------------------------------


def overview_side(db: Session, day: date) -> tuple[dict[int, dict], dict]:
    board = overview.board(db, day, None, None, None, overview.stale_minutes_default())
    people = {r["staff_id"]: {"sos": r["progress"]["total"], "kg": round(r["kg"], 2), "packs": r["packs"], "batches": r["batch_count"]} for r in board["rows"] if r["batch_count"]}
    kpis = overview.summary(db, day, day, None, None)["kpis"]
    return people, kpis


# ---- the check ---------------------------------------------------------------------------------------------------------------


def _equal(field: str, a, b) -> bool:
    if field == "kg":
        return abs((a or 0.0) - (b or 0.0)) <= KG_TOLERANCE
    return a == b


def reconcile(db: Session, day: date, source: str = "auto") -> dict:
    lp, used, lp_missing = load_planning_side(db, day, source)
    rows = day_model.rows_in_range(db, day, day)
    persons = {p for row in rows for p in _crew(row)} | set(lp)
    app = driver_app_side(db, day, persons)
    ov, kpis = overview_side(db, day)
    people = []
    for person in sorted(persons | set(ov)):
        sides = {"load_planning": lp.get(person), "driver_app": app.get(person), "overview": ov.get(person)}
        mismatches = [f for f in FIELDS if not all(_equal(f, (sides[a] or {}).get(f), (sides["driver_app"] or {}).get(f)) for a in ("load_planning", "overview"))]
        people.append({"staff_id": person, "name": _name(person), **sides, "match": not mismatches, "mismatches": mismatches})
    distinct_sos = len({row.id for row in rows})
    kpi_ok = kpis["sos"]["assigned"] == distinct_sos
    return {"date": day.isoformat(), "load_planning_source": used, "lp_missing": lp_missing, "people": people, "overview_kpi_sos_assigned": kpis["sos"]["assigned"], "distinct_sos": distinct_sos,
            "kpi_matches": kpi_ok, "ok": kpi_ok and all(p["match"] for p in people) and not lp_missing}
