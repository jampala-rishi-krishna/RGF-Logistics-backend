"""Turn a ZohoError that no route caught into a readable 503 instead of an unhandled 500.

An unhandled exception is answered by Starlette's outermost ServerErrorMiddleware, i.e. OUTSIDE the CORS middleware, so
the browser gets a response without CORS headers and reports a bare "could not reach the API / CORS" network error.
A handler registered with add_exception_handler runs INSIDE the middleware stack, so the response keeps its CORS headers
and the app can show the real reason (for example "Zoho daily budget guard active; non-essential Zoho calls are paused.").
"""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from services.zoho_client import ZohoError

logger = logging.getLogger("zoho_error_handler")


async def zoho_error_handler(request: Request, exc: ZohoError) -> JSONResponse:
    message = str(exc) or "Zoho is unavailable. Please try again."
    logger.warning("[ZOHO] %s %s -> 503: %s", request.method, request.url.path, message)
    return JSONResponse(status_code=503, content={"detail": message, "code": "zoho_unavailable"})


def register(app) -> None:
    app.add_exception_handler(ZohoError, zoho_error_handler)
