"""CORS configuration, request ids and the last-resort 500 handler, shared by main.py and /ws/fleet.

Why this exists: an exception that escapes every middleware is answered by Starlette's OUTERMOST ServerErrorMiddleware,
which sits outside CORSMiddleware, so the browser sees a bare "CORS / network error" instead of the real 500. Three layers
close that gap:

1. `safe_errors.install_catch_all` (inside CORS) turns uncaught route exceptions into JSON 500s.
2. `server_error_handler` is registered for `Exception`, so even a failure that escapes the middleware stack gets a JSON
   500 with CORS headers (it adds them itself) - this keeps `main:app` safe when Render's dashboard start command is used.
3. `CORSFallbackMiddleware` wraps the whole app (`main:asgi_app`) and adds CORS headers to any response that still lacks
   them. It never duplicates headers the inner CORSMiddleware already set.

Origins come from CORS_ALLOWED_ORIGINS (falls back to the older ALLOWED_ORIGINS). CORS_ALLOW_ORIGIN_REGEX is optional and
only ever accepts the single documented Rare frontend pattern; anything else refuses to start. Wildcards are never allowed
because credentials are allowed.
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Mapping

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("http_hardening")

REQUEST_ID_HEADER = "X-Request-ID"
# The only origin regex the backend will accept: the Rare logistics frontend on Cloudflare Workers / Pages, including
# Pages preview subdomains (<hash>.rare-logistics-frontend.pages.dev).
ALLOWED_ORIGIN_REGEX = r"^https://([a-z0-9-]+\.)?rare-logistics-frontend\.(rishi-315\.workers|pages)\.dev$"

_SANE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


# ---- CORS settings ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CorsSettings:
    origins: tuple[str, ...]
    origin_regex: str | None = None
    _compiled: re.Pattern | None = field(default=None, compare=False, repr=False)

    def allows(self, origin: str | None) -> bool:
        if not origin:
            return False
        origin = origin.strip()
        if origin in self.origins:
            return True
        return bool(self._compiled and self._compiled.fullmatch(origin))

    def middleware_kwargs(self) -> dict:
        return {
            "allow_origins": list(self.origins),
            "allow_origin_regex": self.origin_regex,
            "allow_credentials": True,
            "allow_methods": ["*"],
            "allow_headers": ["*"],
            "expose_headers": [REQUEST_ID_HEADER],
        }


def _normalise_origin(value: str) -> str:
    return value.strip().rstrip("/")


def load_cors_settings(environ: Mapping[str, str] | None = None) -> CorsSettings:
    """Parse and validate the CORS env. Raises RuntimeError on an unsafe or empty configuration."""
    env = os.environ if environ is None else environ
    raw = env.get("CORS_ALLOWED_ORIGINS", "").strip() or env.get("ALLOWED_ORIGINS", "")
    origins: list[str] = []
    for part in raw.split(","):
        origin = _normalise_origin(part)
        if origin and origin not in origins:
            origins.append(origin)
    if "*" in origins:
        raise RuntimeError("CORS_ALLOWED_ORIGINS / ALLOWED_ORIGINS must list exact origins; '*' is not allowed with credentials.")
    regex = (env.get("CORS_ALLOW_ORIGIN_REGEX") or "").strip() or None
    if regex is not None and regex != ALLOWED_ORIGIN_REGEX:
        raise RuntimeError("CORS_ALLOW_ORIGIN_REGEX may only be the documented Rare frontend pattern (see docs/DEPLOY.md); refusing to start.")
    if not origins and regex is None:
        raise RuntimeError("CORS_ALLOWED_ORIGINS (or ALLOWED_ORIGINS) must contain one or more exact origins; wildcard origins are not allowed in production.")
    return CorsSettings(tuple(origins), regex, re.compile(regex) if regex else None)


_active: CorsSettings | None = None


def activate(settings: CorsSettings) -> CorsSettings:
    """Make `settings` the configuration `is_origin_allowed` checks (main.py calls this once at import)."""
    global _active
    _active = settings
    return settings


def is_origin_allowed(origin: str | None, settings: CorsSettings | None = None) -> bool:
    settings = settings or _active
    return bool(settings and settings.allows(origin))


def _apply_cors_headers(headers: MutableHeaders, origin: str) -> None:
    headers["Access-Control-Allow-Origin"] = origin
    headers["Access-Control-Allow-Credentials"] = "true"
    headers["Access-Control-Expose-Headers"] = REQUEST_ID_HEADER
    headers.add_vary_header("Origin")


# ---- Request ids ------------------------------------------------------------------------------------------------------


def current_request_id() -> str | None:
    return _request_id.get()


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def request_id_for(scope) -> str:
    state = scope.get("state") if isinstance(scope, dict) else None
    if isinstance(state, dict) and state.get("request_id"):
        return state["request_id"]
    return current_request_id() or new_request_id()


class RequestIDMiddleware:
    """Pure ASGI: accept a sane incoming X-Request-ID or mint one, expose it on request.state, a contextvar and the response."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "").strip()
        request_id = incoming if _SANE_REQUEST_ID.fullmatch(incoming) else new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        token = _request_id.set(request_id)

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _request_id.reset(token)


# ---- Last-resort error handling ---------------------------------------------------------------------------------------


def server_error_body(request_id: str) -> dict:
    return {"detail": "Server error", "code": "internal_error", "request_id": request_id}


def log_server_error(log: logging.Logger, tag: str, request: Request, request_id: str, exc: BaseException) -> None:
    """Full traceback at ERROR: once multi-line, and once on a single line carrying the request id, because Render
    shows each traceback line as its own log entry and a search for the request id would otherwise miss them."""
    log.error("%s request_id=%s %s %s -> 500 (%s)", tag, request_id, request.method, request.url.path, type(exc).__name__,
              exc_info=(type(exc), exc, exc.__traceback__))
    flat = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip().replace("\n", " ⏎ ")
    log.error("[TRACEBACK] request_id=%s %s %s %s", request_id, request.method, request.url.path, flat)


async def server_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Registered for `Exception`, so Starlette runs it from ServerErrorMiddleware (outside CORS). It therefore adds the
    CORS and request-id headers itself. Never echoes exception text."""
    request_id = request_id_for(request.scope)
    log_server_error(logger, "[ERROR]", request, request_id, exc)
    response = JSONResponse(status_code=500, content=server_error_body(request_id))
    response.headers[REQUEST_ID_HEADER] = request_id
    origin = request.headers.get("origin")
    if origin and is_origin_allowed(origin):
        _apply_cors_headers(response.headers, origin)
    return response


class CORSFallbackMiddleware:
    """Outermost wrapper for `main:asgi_app`. Adds CORS headers to any HTTP response that has none (e.g. a 500 produced by
    Starlette's ServerErrorMiddleware), only for allowed origins. Preflights and normal responses are already handled by
    the app's own CORSMiddleware, so this never duplicates headers."""

    def __init__(self, app, settings: CorsSettings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = Headers(scope=scope).get("origin")
        if not origin or not self.settings.allows(origin):
            await self.app(scope, receive, send)
            return

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "access-control-allow-origin" not in headers:
                    _apply_cors_headers(headers, origin)
            await send(message)

        await self.app(scope, receive, send_with_cors)


def install(app) -> None:
    """Request ids just inside CORS, plus the `Exception` handler. Call AFTER safe_errors.install_catch_all and BEFORE
    adding CORSMiddleware, so the stack is: ServerError -> CORS -> RequestID -> catch_all -> ... -> routes."""
    app.add_middleware(RequestIDMiddleware)
    app.add_exception_handler(Exception, server_error_handler)


def wrap(app, settings: CorsSettings):
    return CORSFallbackMiddleware(app, settings)
