from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.config import Config
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# httpx includes query strings in its INFO request log; Mapbox credentials are query
# parameters, so request URLs must never be emitted by the backend logger.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("main")

from routers import admin, agent, alerts, assignment, auth, comms, dispatch, fleet, gmail, load_planning, optimization, orders, reports, routes, voice, warehouse, pipeline
from services import fleet_static_cache, staff_directory_cache, live_sales_order_cache
from services.cartrack_poller import poll_cartrack_and_update, report_unmatched_roster_on_startup
from services.ws_manager import manager
from auth.security import hash_password, verify_password
import database
from database import SessionLocal, engine
from models.user import User

scheduler = AsyncIOScheduler()

CARTRACK_CONFIGURED = bool(os.environ.get("CARTRACK_USERNAME") and os.environ.get("CARTRACK_API_KEY"))
schema_state = {"ok": False, "current": None, "head": None}

def check_schema() -> None:
    config = Config(os.path.join(os.path.dirname(__file__), "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    head = scripts.get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    schema_state.update(ok=current == head, current=current, head=head)
    if current != head:
        logger.error("[SCHEMA] VERSION MISMATCH: database=%s, code=%s. Run 'python -m alembic upgrade head' from backend with the venv active. Startup stopped before cache queries.", current, head)
        raise SystemExit(2)
        logger.error("[SCHEMA] DB at %s, code expects %s — run alembic upgrade head", current, head)


def repair_demo_accounts() -> None:
    password = os.environ.get("SEED_PASSWORD", "").strip()
    if not password:
        return
    try:
        with SessionLocal() as db:
            accounts = (("dispatcher@rgf.test", "RGF Dispatcher", "dispatcher", 639170000001), ("admin@rgf.test", "RGF Admin", "admin", 639170000002))
            users = []
            for email, full_name, role, phone in accounts:
                user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
                if user is None:
                    user = User(email=email, full_name=full_name, phone=phone, role=role)
                    db.add(user)
                    db.flush()
                # Avoid an expensive bcrypt rehash on every restart. Only repair the
                # account when the configured seed password no longer matches.
                if not user.password_hash or not verify_password(password, user.password_hash):
                    user.password_hash = hash_password(password)
                user.status = "active"
                users.append(user)
            db.commit()
            logger.info("Demo account credentials synchronized for %d configured accounts", len(users))
    except Exception:
        logger.warning("Demo account synchronization skipped; database was unavailable")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # repair_demo_accounts() (inserts/upserts demo Users) stays disabled - the one
    # admin account now comes from scripts/seed_minimal.py, run manually. Nothing else
    # inserts rows on startup.
    try:
        check_schema()
    except SystemExit:
        raise
    except Exception as exc:
        logger.error("[SCHEMA] Unable to verify migration state before cache queries: %s", exc)
        raise SystemExit(2) from exc
    if not schema_state["ok"]:
        logger.error("[SCHEMA] Startup stopped before cache queries; run 'python -m alembic upgrade head'.")
        raise SystemExit(2)
    fleet_static_cache.refresh()
    # Startup fetch only, no timer job (see staff_directory_cache.refresh's docstring) -
    # refresh again via POST /api/dispatch/staff/refresh when staff actually changes.
    staff_directory_cache.refresh()
    # Phase 2b (2026-09-24): the ONLY persisted read on a normal request path. Loads
    # which SOs are currently assigned/manifested/completed from sales_order_history once,
    # so a restart doesn't lose assignment state. Updated in memory from then on.
    with SessionLocal() as db:
        live_sales_order_cache.load_assignment_state_from_history(db)
    # Re-enabled 2026-09-24, scoped: this only ever touches sales_order_history rows for
    # SOs that are already assignment_status == "assigned" (see fleet.refresh_vehicles) -
    # it cannot write an unassigned SO, because unassigned SOs are never stored at all.
    fleet_sync_interval = int(os.environ.get("FLEET_ZOHO_SYNC_INTERVAL_SECONDS", "1800"))
    scheduler.add_job(
        fleet.scheduled_fleet_sync,
        trigger="interval",
        seconds=fleet_sync_interval,
        id="fleet_zoho_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Fleet Zoho sync (assigned SOs only) started - syncing every %ss", fleet_sync_interval)
    if CARTRACK_CONFIGURED:
        poll_interval = int(os.environ.get("CARTRACK_POLL_INTERVAL_SECONDS", "5"))
        scheduler.add_job(
            poll_cartrack_and_update,
            trigger="interval",
            seconds=poll_interval,
            id="cartrack_gps_poll",
            replace_existing=True,
            max_instances=1,
        )
        # Nothing is subscribed yet at startup - pause immediately, the first WS
        # connection resumes it, and the poller pauses again once the last client
        # disconnects. No Cartrack calls (and no DB egress) happen while idle.
        scheduler.pause_job("cartrack_gps_poll")
        manager.on_first_connect = lambda: scheduler.resume_job("cartrack_gps_poll")
        manager.on_last_disconnect = lambda: scheduler.pause_job("cartrack_gps_poll")
        logger.info(f"Cartrack GPS poller registered - polling every {poll_interval}s while >=1 WS client is connected")
        asyncio.create_task(report_unmatched_roster_on_startup())
    else:
        logger.warning("CARTRACK_USERNAME/CARTRACK_API_KEY not configured - GPS poller disabled")

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(title="IntelliFleet API", lifespan=lifespan)

# Single CORS configuration point for the whole app - every former Catalyst function had its
# own (no-op) CORS handling; this is the one place it happens now.
allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if not allowed_origins or "*" in allowed_origins:
    raise RuntimeError("ALLOWED_ORIGINS must contain one or more exact origins; wildcard origins are not allowed in production.")
logger.info("Allowed CORS origins: %s", allowed_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

media_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
os.makedirs(media_root, exist_ok=True)
app.mount("/media", StaticFiles(directory=media_root), name="media")

app.include_router(auth.router)
app.include_router(fleet.router)
app.include_router(orders.router)
app.include_router(alerts.router)
app.include_router(routes.router)
app.include_router(comms.router)
app.include_router(warehouse.router)
app.include_router(admin.router)
app.include_router(agent.router)
app.include_router(optimization.router)
app.include_router(load_planning.router)
app.include_router(assignment.optimize_router)
app.include_router(assignment.router)
app.include_router(pipeline.router)
app.include_router(reports.router)
app.include_router(dispatch.router)
app.include_router(dispatch.public_router)
app.include_router(gmail.router)
app.include_router(voice.router)
app.include_router(voice.webhook_router)
app.include_router(voice.admin_router)


@app.get("/health")
def health():
    if not schema_state["ok"]:
        return JSONResponse(status_code=503, content={"status": "schema_mismatch", **schema_state})
    return {
        "status": "ok",
        "connected_ws_clients": manager.connection_count,
        "cartrack_poller": CARTRACK_CONFIGURED,
        "cartrack_poller_active": bool(CARTRACK_CONFIGURED and manager.connection_count > 0),
        "db_egress": database.db_egress_stats,
    }

@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError):
    return JSONResponse(status_code=500, content={"error": "Database operation failed", "detail": str(exc)})


@app.post("/poll-now")
async def poll_now():
    """Manual trigger for verification - runs one poll cycle immediately."""
    if not CARTRACK_CONFIGURED:
        return {"status": "skipped", "reason": "Cartrack not configured"}
    await poll_cartrack_and_update()
    return {"status": "ok"}


@app.websocket("/ws/fleet")
async def ws_fleet(websocket: WebSocket):
    # Unauthenticated for v1, matching every other module's existing posture of leaving
    # read/GET-equivalent surfaces open and gating only writes.
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
