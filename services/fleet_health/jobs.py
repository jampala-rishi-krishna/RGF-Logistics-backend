"""APScheduler wiring for Fleet Health (the existing scheduler in main.py)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from database import SessionLocal
from services.fleet_health import config, sampler, service

logger = logging.getLogger("fleet_health.jobs")


async def nightly_job() -> None:
    """01:00 Manila: compute yesterday. Never raises into the scheduler; re-running a day just overwrites its rows."""
    try:
        with SessionLocal() as db:
            result = await service.run_nightly(db)
        logger.info("[FLEET_HEALTH] nightly done: %s calls=%s", result.get("day"), result.get("cartrack_calls"))
    except Exception:  # noqa: BLE001
        logger.exception("[FLEET_HEALTH] nightly job failed (will not retry until tomorrow; run the backfill to repair)")


async def scorecard_job() -> None:
    """Monday 08:00 Manila: weekly eco scorecard over WhatsApp. A no-op unless an admin switched it on."""
    try:
        from services.fleet_health import scorecard

        with SessionLocal() as db:
            result = await scorecard.send_weekly(db)
        logger.info("[FLEET_HEALTH] scorecard job: %s", result)
    except Exception:  # noqa: BLE001
        logger.exception("[FLEET_HEALTH] scorecard job failed")


def register(scheduler) -> None:
    scheduler.add_job(
        sampler.sample_once, trigger="interval", seconds=config.SAMPLE_INTERVAL_SECONDS, id="fleet_health_sampler",
        replace_existing=True, max_instances=1, coalesce=True, next_run_time=datetime.now(config.MANILA) + timedelta(seconds=30),
    )
    scheduler.add_job(
        nightly_job, trigger="cron", hour=config.NIGHTLY_HOUR, minute=config.NIGHTLY_MINUTE, timezone=config.MANILA,
        id="fleet_health_nightly", replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    scheduler.add_job(
        scorecard_job, trigger="cron", day_of_week="mon", hour=config.SCORECARD_HOUR, minute=0, timezone=config.MANILA,
        id="fleet_health_scorecard", replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    logger.info("[FLEET_HEALTH] sampler every %ss and nightly job at %02d:%02d Asia/Manila registered", config.SAMPLE_INTERVAL_SECONDS, config.NIGHTLY_HOUR, config.NIGHTLY_MINUTE)
