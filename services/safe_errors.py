"""Every backend failure must reach the browser WITH CORS headers, so the app can show the real reason instead of a bare
"could not reach the API / CORS" network error.

Starlette answers an unhandled exception from its OUTERMOST ServerErrorMiddleware, i.e. outside CORSMiddleware, so that response has no
CORS headers. Two pieces fix it: (1) exception handlers for the expected failures (ZohoError -> 503 zoho_unavailable, WindowWarming ->
503 warming + Retry-After), which run inside the middleware stack; (2) `install_catch_all`, an HTTP middleware that MUST be added BEFORE
CORSMiddleware (so it sits inside it) and turns any other uncaught exception into a JSON 500.
"""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from services.http_hardening import REQUEST_ID_HEADER, log_server_error, request_id_for, server_error_body
from services.live_sales_order_cache import WindowWarming

logger = logging.getLogger("safe_errors")


async def warming_handler(request: Request, exc: WindowWarming) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc) or "Loading. Try again in a few seconds.", "code": "warming"}, headers={"Retry-After": "5"})


def register(app) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    app.add_exception_handler(WindowWarming, warming_handler)
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_error_handler)


def install_catch_all(app) -> None:
    @app.middleware("http")
    async def catch_all(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            # Full traceback goes to the log with the request id; the browser only gets the id, never the exception text.
            request_id = request_id_for(request.scope)
            log_server_error(logger, "[ERROR]", request, request_id, exc)
            return JSONResponse(status_code=500, content=server_error_body(request_id), headers={REQUEST_ID_HEADER: request_id})


async def sqlalchemy_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Database failures: log the details server-side, never return str(exc) (it can contain SQL, parameters and hosts)."""
    request_id = request_id_for(request.scope)
    log_server_error(logger, "[DB]", request, request_id, exc)
    return JSONResponse(status_code=500, content={"error": "Database operation failed", **server_error_body(request_id)},
                        headers={REQUEST_ID_HEADER: request_id})
