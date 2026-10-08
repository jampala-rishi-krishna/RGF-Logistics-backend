"""Backfill vehicle_daily_stats from Cartrack trips.   DRY RUN by default (writes nothing).

    python -m scripts.fleet_health_backfill                 # dry run, last 30 days, prints sample rows + call count
    python -m scripts.fleet_health_backfill --days 30 --write
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
os.environ.setdefault("JWT_SECRET", "cli")

from database import SessionLocal  # noqa: E402
from services.fleet_health import service  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--write", action="store_true", help="actually write rows (default is a dry run)")
    parser.add_argument("--sample", type=int, default=3)
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="last day to include, YYYY-MM-DD (default: yesterday, Manila)")
    args = parser.parse_args()
    with SessionLocal() as db:
        result = await service.run_backfill(db, args.days, dry_run=not args.write, end=args.end)
    rows = result.pop("rows")
    print(json.dumps({**result, "sample_rows": rows[: args.sample]}, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
