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

from services.live_sales_order_cache import WindowWarming

logger = logging.getLogger("safe_errors")


async def warming_handler(request: Request, exc: WindowWarming) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc) or "Loading. Try again in a few seconds.", "code": "warming"}, headers={"Retry-After": "5"})


def register(app) -> None:
    app.add_exception_handler(WindowWarming, warming_handler)


def install_catch_all(app) -> None:
    @app.middleware("http")
    async def catch_all(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[ERROR] %s %s -> 500 (%s)", request.method, request.url.path, type(exc).__name__)
            return JSONResponse(status_code=500, content={"detail": "The server hit an unexpected error. Please try again.", "code": "internal_error", "error_type": type(exc).__name__})
