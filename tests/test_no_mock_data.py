"""No dummy / static data in the driver app, the Driver Overview or the officer screens (rebuild quality bar).

Scans the SOURCE of those screens and of their backend read models for the strings that appeared in the old mock-ups. Test fixtures (tests/, e2e/) and
real reference data elsewhere in the repo (real truck plates, the real warehouse address) are out of scope: the rule is about what a driver or dispatcher
is shown as if it were their data.
"""
from __future__ import annotations

import re
from pathlib import Path

FORBIDDEN = ("Ramon", "Cavite", "IF-204", "DCD 8953", "ABC Mart", "ORD-0253", "Juan Dela Cruz")
BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
FRONTEND = REPO / "artifacts" / "intellifleet" / "src"

SCOPES = [
    *(FRONTEND / "pages" / d for d in ("driver", "overview", "officer")),
    FRONTEND / "services" / "api" / "driver.ts",
    FRONTEND / "services" / "api" / "overview.ts",
    FRONTEND / "services" / "api" / "officer.ts",
    BACKEND / "routers" / "driver.py", BACKEND / "routers" / "driver_trip.py", BACKEND / "routers" / "driver_extra.py", BACKEND / "routers" / "driver_overview.py", BACKEND / "routers" / "officer.py",
    *(BACKEND / "services" / f"{n}.py" for n in ("day_model", "driver_assignments", "driver_alerts", "driver_issues", "overview", "reconcile", "trip_state", "trip_timeline", "trip_workflow", "handover", "pack_units", "weight_display")),
]
LIB = [FRONTEND / "lib" / n for n in ("driver.ts", "overview.ts", "overviewView.ts", "tripCards.ts", "tripTypes.ts", "tripForms.ts", "alerts.ts", "issues.ts", "syncStatus.ts", "officer.ts")]


def _files():
    for scope in [*SCOPES, *LIB]:
        if scope.is_dir():
            yield from (p for p in scope.rglob("*") if p.suffix in {".ts", ".tsx"} and ".test." not in p.name)
        elif scope.exists():
            yield scope


def test_known_mock_strings_are_not_in_driver_overview_or_officer_code():
    hits = []
    for path in _files():
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if re.search(re.escape(needle), text):
                hits.append(f"{path.relative_to(REPO)}: {needle}")
    assert hits == []


def test_the_scan_really_covers_files():
    assert sum(1 for _ in _files()) > 40
