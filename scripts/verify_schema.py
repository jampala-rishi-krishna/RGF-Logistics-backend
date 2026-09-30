"""One-off verification script: prove every table+column exists in Neon via a real query."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

engine = create_engine(os.environ["DATABASE_URL"])

with engine.connect() as conn:
    tables = conn.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
    ).fetchall()
    print(f"=== {len(tables)} tables in public schema ===")
    for (t,) in tables:
        print(f" - {t}")

    print()
    for check_table, check_cols in [
        ("vehicles", ["plate_no", "driver_id"]),
        ("route_stops", ["arrival_window_end"]),
        ("customers", ["latitude", "longitude"]),
        ("users", ["password_hash", "email", "phone"]),
        ("vehicle_operating_profiles", ["capacity_kg", "depot_lat", "depot_lng"]),
        ("optimization_runs", ["data_snapshot", "scope"]),
    ]:
        cols = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"
            ),
            {"t": check_table},
        ).fetchall()
        col_map = {c: dtype for c, dtype in cols}
        print(f"=== {check_table} ({len(cols)} columns) ===")
        for c, dtype in cols:
            print(f"   {c}: {dtype}")
        for expect in check_cols:
            status = "OK" if expect in col_map else "MISSING"
            print(f"   -> expected column '{expect}': {status} ({col_map.get(expect)})")
        print()
