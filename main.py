from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
import logging
import os
import socket
import uuid
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from auth.dependencies import require_role, workspace_access
from fastapi import Request
from fastapi.responses import JSONResponse
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

from routers import access_workflow, pickup, admin, agent, alerts, assignment, auth, comms, communications, confirmed_email, dispatch, driver, driver_extra, driver_overview, driver_trip, fleet, fleet_health, gmail, load_planning, media, officer, optimization, orders, reports, routes, voice, warehouse, pipeline, zoho_webhooks, order_tracker
from services.fleet_health import sampler as fleet_health_sampler
from services import fleet_static_cache, gmail_sender, staff_directory_cache, live_sales_order_cache, voice_control, whatsapp_control, zoho_so_lock, zoho_usage
from services.cartrack_poller import poll_cartrack_and_update, report_unmatched_roster_on_startup
from services.ws_manager import manager
from services import http_hardening, memory_debug
from auth.security import decode_access_token
from auth.security import hash_password, verify_password
import database
from database import SessionLocal, engine
from models.user import User

scheduler = AsyncIOScheduler()

CARTRACK_CONFIGURED = bool(os.environ.get("CARTRACK_USERNAME") and os.environ.get("CARTRACK_API_KEY"))
schema_state = {"ok": False, "current": None, "head": None}
INSTANCE_ID = os.environ.get("RENDER_INSTANCE_ID") or os.environ.get("HOSTNAME") or socket.gethostname() or str(uuid.uuid4())


def configured_worker_count() -> int:
    for key in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "WORKERS"):
        value = os.environ.get(key)
        if value:
            try:
                return int(value)
            except ValueError:
                logger.error("[CONFIG] invalid %s=%r", key, value)
                return 1
    return 1

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
    from services import agent_tools
    workers = configured_worker_count()
    if workers > 1:
        logger.error("[CONFIG] multiple workers detected, acknowledge state is per-process")
    agent_tools.log_internal_api_config()
    # Restore today's Zoho call total from Neon BEFORE anything may call Zoho, so the counter and the
    # budget guard survive restarts (fails safe if Neon is unreachable). Flushes every 60s and at lifespan shutdown (uvicorn's own SIGTERM handling is untouched).
    # Admin-editable settings (budget, dispatch phone, ...): one read of the small app_settings table, BEFORE the
    # Zoho counter starts so the budget guard uses the saved budget. Env values stay in force if this fails.
    from services import app_settings
    app_settings.load()
    zoho_usage.start(INSTANCE_ID)
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
        from services import purchase_orders
        purchase_orders.load_decisions(db)
    # Build the Fleet cache once in the background (assigned SOs only) so the first Fleet view after a
    # deploy is instant. /vehicles shares a single-flight lock with this, so it never double-fetches.
    threading.Thread(target=fleet.warm_fleet_cache, name="fleet-cache-warmup", daemon=True).start()  # no-op under LOCAL_DISABLE_ZOHO_WARMUP
    # Re-enabled 2026-09-24, scoped: this only ever touches sales_order_history rows for
    # SOs that are already assignment_status == "assigned" (see fleet.refresh_vehicles) -
    # it cannot write an unassigned SO, because unassigned SOs are never stored at all.
    fleet_sync_interval = int(os.environ.get("FLEET_ZOHO_SYNC_INTERVAL_SECONDS", "3600"))
    if live_sales_order_cache.local_zoho_warmup_disabled():
        logger.warning("LOCAL_DISABLE_ZOHO_WARMUP is on: no Fleet warm-up, no scheduled Zoho fleet sync, Fleet builds from memory only")
    else:
        scheduler.add_job(
            fleet.scheduled_fleet_sync,
            trigger="interval",
            seconds=fleet_sync_interval,
            id="fleet_zoho_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    from services import pickup_email
    scheduler.add_job(pickup_email.scheduled_tick, "interval", seconds=30, id="pickup_email", max_instances=1, coalesce=True)
    scheduler.start()
    memory_task = memory_debug.start_periodic_logging()  # no-op unless MEMORY_DEBUG_LOG=1
    gmail_sender.log_identity_config_at_startup()
    zoho_so_lock.log_configuration_warning()
    # Inbound Logistics email agent (replaces the n8n agent). OFF unless LOGISTICS_AGENT_ENABLED=true,
    # so it never answers alongside the n8n workflow before cutover.
    from services import logistics_email_agent
    if logistics_email_agent.enabled():
        scheduler.add_job(
            logistics_email_agent.poll_once,
            trigger="interval",
            seconds=int(os.environ.get("LOGISTICS_AGENT_POLL_SECONDS", "60")),
            id="logistics_email_agent",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("Logistics email agent enabled - polling Gmail every %ss", os.environ.get("LOGISTICS_AGENT_POLL_SECONDS", "60"))
    if not live_sales_order_cache.local_zoho_warmup_disabled():
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
        def _start_gps_polling() -> None:
            # Resume AND poll right now: resuming alone would wait one full interval for the first positions.
            scheduler.resume_job("cartrack_gps_poll")
            scheduler.modify_job("cartrack_gps_poll", next_run_time=datetime.now(timezone.utc))

        manager.on_first_connect = _start_gps_polling
        manager.on_last_disconnect = lambda: scheduler.pause_job("cartrack_gps_poll")
        logger.info(f"Cartrack GPS poller registered - polling every {poll_interval}s while >=1 WS client is connected")
        asyncio.create_task(report_unmatched_roster_on_startup())
        # Fleet Health: status sampler (ONE /rest/vehicles/status call per 10 min) and the 01:00 Manila nightly job.
        from services.fleet_health import jobs as fleet_health_jobs
        fleet_health_jobs.register(scheduler)
    else:
        logger.warning("CARTRACK_USERNAME/CARTRACK_API_KEY not configured - GPS poller disabled")

    yield

    if memory_task is not None:
        memory_task.cancel()
    scheduler.shutdown(wait=False)
    zoho_usage.stop()


app = FastAPI(title="IntelliFleet API", lifespan=lifespan)


@app.middleware("http")
async def zoho_request_context(request, call_next):
    from services.zoho_acquisition import request_context
    # No query string, user credentials, cookies or headers enter acquisition logs.
    with request_context(request.url.path):
        return await call_next(request)

# Any uncaught exception -> JSON 500 that still passes through CORS (this must be registered BEFORE CORSMiddleware so it sits inside it).
# Also handles SQLAlchemyError (no raw DB error text) and WindowWarming.
from services import safe_errors  # noqa: E402
safe_errors.install_catch_all(app)
safe_errors.register(app)
# X-Request-ID on every response + a last-resort Exception handler that adds CORS headers itself, so `main:app` alone is
# safe even if Render's dashboard start command still says main:app. Stack: ServerError -> CORS -> RequestID -> catch_all.
http_hardening.install(app)

# Single CORS configuration point for the whole app. CORS_ALLOWED_ORIGINS (exact origins, comma-separated) falls back to
# the older ALLOWED_ORIGINS; CORS_ALLOW_ORIGIN_REGEX optionally allows the documented Rare frontend pattern only.
cors_settings = http_hardening.activate(http_hardening.load_cors_settings())
allowed_origins = list(cors_settings.origins)
logger.info("Allowed CORS origins: %s regex: %s", allowed_origins, cors_settings.origin_regex or "none")
app.add_middleware(
    CORSMiddleware,
    **cors_settings.middleware_kwargs(),
)

media_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
os.makedirs(media_root, exist_ok=True)
app.mount("/media", StaticFiles(directory=media_root), name="media")

@app.middleware("http")
async def protect_assignment_attribution(request: Request, call_next):
    from services.assignment_privacy import redact
    from auth.dependencies import _load_user
    from fastapi.concurrency import run_in_threadpool
    from starlette.responses import Response
    import json
    response = await call_next(request)
    if "application/json" not in response.headers.get("content-type", ""):
        return response
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    payload = decode_access_token(token) if token else None
    user = await run_in_threadpool(_load_user, int(payload["sub"])) if payload else None
    manager = bool(user and user.is_manager) and not any(part in request.url.path for part in ("/export", "/email", "/pickup"))
    body = b"".join([chunk async for chunk in response.body_iterator])
    try:
        body = json.dumps(redact(json.loads(body), manager), ensure_ascii=False).encode()
    except (ValueError, TypeError):
        pass
    headers = dict(response.headers)
    headers.pop("content-length", None)
    return Response(body, status_code=response.status_code, headers=headers, background=response.background)


app.include_router(access_workflow.router, dependencies=[Depends(workspace_access)])
app.include_router(order_tracker.router, dependencies=[Depends(workspace_access)])
app.include_router(pickup.router, dependencies=[Depends(workspace_access)])
app.include_router(auth.router, dependencies=[Depends(workspace_access)])
app.include_router(driver.router, dependencies=[Depends(workspace_access)])
app.include_router(driver_trip.router, dependencies=[Depends(workspace_access)])
app.include_router(driver_extra.router, dependencies=[Depends(workspace_access)])
app.include_router(officer.router, dependencies=[Depends(workspace_access)])
app.include_router(driver_overview.router, dependencies=[Depends(workspace_access)])
app.include_router(media.router, dependencies=[Depends(workspace_access)])
app.include_router(fleet.router, dependencies=[Depends(workspace_access)])
app.include_router(orders.router, dependencies=[Depends(workspace_access)])
app.include_router(alerts.router, dependencies=[Depends(workspace_access)])
app.include_router(routes.router, dependencies=[Depends(workspace_access)])
app.include_router(comms.router, dependencies=[Depends(workspace_access)])
app.include_router(warehouse.router, dependencies=[Depends(workspace_access)])
app.include_router(admin.router, dependencies=[Depends(workspace_access)])
app.include_router(admin.usage_router, dependencies=[Depends(workspace_access)])
app.include_router(agent.router, dependencies=[Depends(workspace_access)])
app.include_router(optimization.router, dependencies=[Depends(workspace_access)])
app.include_router(load_planning.router, dependencies=[Depends(workspace_access)])
app.include_router(confirmed_email.router, dependencies=[Depends(workspace_access)])
app.include_router(assignment.optimize_router, dependencies=[Depends(workspace_access)])
app.include_router(assignment.router, dependencies=[Depends(workspace_access)])
app.include_router(pipeline.router, dependencies=[Depends(workspace_access)])
app.include_router(fleet_health.router, dependencies=[Depends(workspace_access)])
app.include_router(reports.router, dependencies=[Depends(workspace_access)])
app.include_router(dispatch.router, dependencies=[Depends(workspace_access)])
app.include_router(communications.router, dependencies=[Depends(workspace_access)])
app.include_router(dispatch.public_router, dependencies=[Depends(workspace_access)])
app.include_router(gmail.router, dependencies=[Depends(workspace_access)])
app.include_router(voice.router, dependencies=[Depends(workspace_access)])
app.include_router(voice.webhook_router, dependencies=[Depends(workspace_access)])
app.include_router(voice.admin_router, dependencies=[Depends(workspace_access)])
app.include_router(zoho_webhooks.router, dependencies=[Depends(workspace_access)])
app.include_router(zoho_webhooks.admin_router, dependencies=[Depends(workspace_access)])


def health_payload():
    """Memory-only /health body (no DB, no network, no blocking locks held across I/O)."""
    if not schema_state["ok"]:
        return JSONResponse(status_code=503, content={"status": "schema_mismatch", **schema_state, "workers": configured_worker_count(), "instance_id": INSTANCE_ID, "rss_mb": memory_debug.rss_mb()})
    zoho_snapshot = zoho_usage.snapshot()
    return {
        "status": "ok",
        "rss_mb": memory_debug.rss_mb(),
        "schema_revision": schema_state["current"],
        "workers": configured_worker_count(),
        "instance_id": INSTANCE_ID,
        "connected_ws_clients": manager.connection_count,
        "cartrack_poller": CARTRACK_CONFIGURED,
        "cartrack_poller_active": bool(CARTRACK_CONFIGURED and manager.connection_count > 0),
        "db_egress": database.db_egress_stats,
        "gmail_identity": gmail_sender.identity_health(),
        "so_lock": zoho_so_lock.health_status(),
        "gmail_auth": {"connected": gmail_sender._auth["ok"], "error": gmail_sender._auth["error"]},  # last token refresh result, memory only
        "voice_calls": voice_control.mode(),
        "fleet_health_sampler": fleet_health_sampler.health(),
        "whatsapp_messages": whatsapp_control.mode(),
        "process_started_at": zoho_snapshot["process_started_at"],
        "usage_restored_from_db": zoho_snapshot["usage_restored_from_db"],
        "zoho_calls_today": zoho_snapshot["zoho_calls_today"],
        "zoho_usage": zoho_snapshot,
    }


@app.get("/health")
async def health():
    # async on purpose: a sync /health shares the AnyIO threadpool with blocking Zoho handlers and can hang when that
    # pool is saturated, which makes Render restart a healthy instance. Everything in health_payload is memory-only.
    return health_payload()

# A ZohoError no route caught (e.g. the budget guard refusing a list call) -> readable 503 that keeps its CORS headers.
from services import zoho_error_handler  # noqa: E402
zoho_error_handler.register(app)


@app.post("/poll-now")
async def poll_now(_user=Depends(require_role("admin", "dispatcher"))):
    """Manual trigger for verification - runs one poll cycle immediately."""
    if not CARTRACK_CONFIGURED:
        return {"status": "skipped", "reason": "Cartrack not configured"}
    await poll_cartrack_and_update()
    return {"status": "ok"}


WS_CLOSE_TOKEN_INVALID = 4401
WS_CLOSE_ORIGIN_FORBIDDEN = 4403


@app.websocket("/ws/fleet")
async def ws_fleet(websocket: WebSocket):
    # Always accept first, then close with an application code: a raise before accept surfaces in the browser as an
    # opaque 1006 with no reason. Origin: browsers always send it, so a disallowed one is refused; a missing Origin
    # (non-browser client) is allowed. Token: optional `?token=<jwt>`; when present it must be valid. When absent the
    # socket is still allowed - the currently deployed frontend sends no token (backward compatibility, read-only feed).
    await websocket.accept()
    origin = websocket.headers.get("origin")
    if origin and not http_hardening.is_origin_allowed(origin):
        logger.warning("[WS] refused /ws/fleet from a disallowed origin")
        await websocket.close(code=WS_CLOSE_ORIGIN_FORBIDDEN, reason="origin not allowed")
        return
    token = websocket.query_params.get("token")
    if token is not None and decode_access_token(token) is None:
        await websocket.close(code=WS_CLOSE_TOKEN_INVALID, reason="token expired")
        return
    try:
        if not manager.register(websocket):
            await websocket.send_json({"type": "status", "cartrack": "unavailable"})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("[WS] /ws/fleet closed after %s", type(exc).__name__)
    finally:
        manager.disconnect(websocket)


# Outermost wrapper (render.yaml starts `main:asgi_app`): adds CORS headers to the few responses that still lack them,
# e.g. a 500 from Starlette's ServerErrorMiddleware. Never duplicates the app's own CORS headers.
asgi_app = http_hardening.wrap(app, cors_settings)
