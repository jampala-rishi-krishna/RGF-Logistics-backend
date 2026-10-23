"""500s keep CORS + a request id, origin config is strict, /ws/fleet never fails before accept, /health is async.
Everything is local: no Zoho, no Cartrack, no database."""
import inspect
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost")
os.environ.setdefault("JWT_SECRET", "test")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jose import jwt  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import main  # noqa: E402
from auth import security  # noqa: E402
from services import http_hardening, memory_debug, safe_errors  # noqa: E402
from services.http_hardening import ALLOWED_ORIGIN_REGEX, load_cors_settings  # noqa: E402

ORIGIN = main.cors_settings.origins[0]
WORKERS_ORIGIN = "https://rare-logistics-frontend.rishi-315.workers.dev"
PAGES_ORIGIN = "https://abc123.rare-logistics-frontend.pages.dev"
EVIL = "https://evil.example"


# ---- 500s through the real main app -----------------------------------------------------------------------------------


@pytest.fixture
def broken_health(monkeypatch):
    monkeypatch.setitem(main.schema_state, "ok", True)

    def boom():
        raise RuntimeError("secret connection string postgres://user:pw@host")

    monkeypatch.setattr(main.zoho_usage, "snapshot", boom)


@pytest.mark.parametrize("target", ["app", "asgi_app"])
def test_route_that_raises_returns_json_500_with_cors_and_request_id(broken_health, target):
    client = TestClient(getattr(main, target), raise_server_exceptions=False)
    response = client.get("/health", headers={"Origin": ORIGIN})
    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Server error" and body["code"] == "internal_error" and body["request_id"]
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["x-request-id"] == body["request_id"]
    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()
    assert "secret" not in response.text and "postgres" not in response.text
    assert response.headers.get_list("access-control-allow-origin") == [ORIGIN]  # never duplicated by asgi_app


def test_incoming_request_id_is_kept_when_sane_and_replaced_when_not(monkeypatch):
    monkeypatch.setitem(main.schema_state, "ok", True)
    client = TestClient(main.app)
    assert client.get("/health", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    replaced = client.get("/health", headers={"X-Request-ID": "bad id\twith <html>"}).headers["x-request-id"]
    assert replaced != "bad id\twith <html>" and len(replaced) == 16


def test_asgi_app_does_not_duplicate_cors_headers_on_normal_responses(monkeypatch):
    monkeypatch.setitem(main.schema_state, "ok", True)
    response = TestClient(main.asgi_app).get("/health", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert response.headers.get_list("access-control-allow-origin") == [ORIGIN]
    assert response.headers["vary"].count("Origin") == 1
    assert response.headers["access-control-expose-headers"].lower() == "x-request-id"
    assert response.headers["x-request-id"]


@pytest.mark.parametrize("target", ["app", "asgi_app"])
def test_500_logs_full_traceback_at_error_on_a_line_with_the_request_id(broken_health, target, caplog):
    client = TestClient(getattr(main, target), raise_server_exceptions=False)
    with caplog.at_level("ERROR"):
        response = client.get("/health", headers={"X-Request-ID": "474f0ac49b404215"})
    assert response.status_code == 500 and response.json()["request_id"] == "474f0ac49b404215"
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any(r.exc_info for r in errors)  # multi-line traceback still logged
    flat = [r.getMessage() for r in errors if r.getMessage().startswith("[TRACEBACK] request_id=474f0ac49b404215")]
    assert len(flat) == 1 and "\n" not in flat[0]
    assert "Traceback (most recent call last)" in flat[0] and "in boom" in flat[0] and "RuntimeError" in flat[0]


def test_sqlalchemy_error_logs_single_line_traceback(caplog):
    app = FastAPI()
    safe_errors.install_catch_all(app)
    safe_errors.register(app)

    @app.get("/db")
    def db():
        raise OperationalError("SELECT 1", {}, Exception("neon down"))

    with caplog.at_level("ERROR"):
        rid = TestClient(app, raise_server_exceptions=False).get("/db").json()["request_id"]
    assert any(r.getMessage().startswith(f"[TRACEBACK] request_id={rid} GET /db") and "OperationalError" in r.getMessage()
               for r in caplog.records)


def test_sqlalchemy_errors_never_leak_their_text():
    app = FastAPI()
    safe_errors.install_catch_all(app)
    safe_errors.register(app)

    @app.get("/db")
    def db():
        raise OperationalError("SELECT secret_column FROM users", {}, Exception("could not connect to neon-host"))

    response = TestClient(app, raise_server_exceptions=False).get("/db")
    assert response.status_code == 500 and response.json()["request_id"]
    assert "secret_column" not in response.text and "neon-host" not in response.text


# ---- The ServerErrorMiddleware path (an exception that escapes every middleware) --------------------------------------


def _escaping_app(settings, with_handler: bool) -> FastAPI:
    """No catch_all: the exception reaches Starlette's outermost ServerErrorMiddleware, outside CORS."""
    app = FastAPI()
    if with_handler:
        app.add_exception_handler(Exception, http_hardening.server_error_handler)
    app.add_middleware(CORSMiddleware, **settings.middleware_kwargs())

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret")

    return app


def test_exception_handler_adds_cors_itself_on_server_error_middleware_500s(monkeypatch):
    settings = load_cors_settings({"CORS_ALLOWED_ORIGINS": ORIGIN})
    monkeypatch.setattr(http_hardening, "_active", settings)
    response = TestClient(_escaping_app(settings, True), raise_server_exceptions=False).get("/boom", headers={"Origin": ORIGIN})
    assert response.status_code == 500 and response.json()["detail"] == "Server error"
    assert response.headers["access-control-allow-origin"] == ORIGIN and "secret" not in response.text


def test_outer_wrapper_adds_cors_to_a_bare_server_error_500():
    settings = load_cors_settings({"CORS_ALLOWED_ORIGINS": ORIGIN})
    wrapped = http_hardening.wrap(_escaping_app(settings, False), settings)
    response = TestClient(wrapped, raise_server_exceptions=False).get("/boom", headers={"Origin": ORIGIN})
    assert response.status_code == 500 and response.headers["access-control-allow-origin"] == ORIGIN
    evil = TestClient(wrapped, raise_server_exceptions=False).get("/boom", headers={"Origin": EVIL})
    assert "access-control-allow-origin" not in evil.headers


# ---- Origin configuration + preflights ---------------------------------------------------------------------------------


def _configured_app(settings) -> FastAPI:
    app = FastAPI()
    safe_errors.install_catch_all(app)
    safe_errors.register(app)
    http_hardening.install(app)
    app.add_middleware(CORSMiddleware, **settings.middleware_kwargs())

    @app.get("/ok")
    def ok():
        return {"ok": True}

    return http_hardening.wrap(app, settings)


@pytest.fixture
def regex_settings(monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", " https://app.example.com/ , http://localhost:5173 ")
    monkeypatch.setenv("CORS_ALLOW_ORIGIN_REGEX", ALLOWED_ORIGIN_REGEX)
    return load_cors_settings()


@pytest.mark.parametrize("origin", [WORKERS_ORIGIN, PAGES_ORIGIN, "https://rare-logistics-frontend.pages.dev", "https://app.example.com"])
def test_preflight_from_allowed_origins(regex_settings, origin):
    client = TestClient(_configured_app(regex_settings))
    response = client.options("/ok", headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                              "Access-Control-Request-Headers": "authorization,content-type"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
    assert http_hardening.is_origin_allowed(origin, regex_settings)


@pytest.mark.parametrize("origin", [EVIL, "https://rare-logistics-frontend.pages.dev.evil.example", "http://rare-logistics-frontend.pages.dev",
                                    "https://other.rishi-315.workers.dev"])
def test_disallowed_origins_get_no_allow_header(regex_settings, origin):
    client = TestClient(_configured_app(regex_settings))
    preflight = client.options("/ok", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in preflight.headers
    simple = client.get("/ok", headers={"Origin": origin})
    assert "access-control-allow-origin" not in simple.headers
    assert not http_hardening.is_origin_allowed(origin, regex_settings)


def test_origin_parsing_and_validation():
    settings = load_cors_settings({"CORS_ALLOWED_ORIGINS": "https://a.example/, https://b.example ,", "ALLOWED_ORIGINS": "https://old.example"})
    assert settings.origins == ("https://a.example", "https://b.example") and settings.origin_regex is None
    assert load_cors_settings({"ALLOWED_ORIGINS": "https://old.example/"}).origins == ("https://old.example",)  # compat
    for bad in ({"ALLOWED_ORIGINS": "*"}, {"CORS_ALLOWED_ORIGINS": ""}, {"ALLOWED_ORIGINS": "https://a.example", "CORS_ALLOW_ORIGIN_REGEX": ".*"},
                {"ALLOWED_ORIGINS": "https://a.example", "CORS_ALLOW_ORIGIN_REGEX": r"^https://.*\.pages\.dev$"}):
        with pytest.raises(RuntimeError):
            load_cors_settings(bad)
    assert load_cors_settings({"CORS_ALLOW_ORIGIN_REGEX": ALLOWED_ORIGIN_REGEX}).origins == ()


# ---- /ws/fleet ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def ws_state(monkeypatch):
    monkeypatch.setattr(main.manager, "_connections", [])
    monkeypatch.setattr(main.manager, "_poller_running", False)
    monkeypatch.setattr(main.manager, "on_first_connect", None)
    monkeypatch.setattr(main.manager, "on_last_disconnect", None)
    return main.manager


def _token(minutes: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return jwt.encode({"sub": "1", "role": "dispatcher", "exp": exp}, security.JWT_SECRET, algorithm=security.JWT_ALGORITHM)


def test_ws_with_expired_token_is_accepted_then_closed_4401(ws_state):
    with TestClient(main.app).websocket_connect(f"/ws/fleet?token={_token(-5)}") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4401 and closed.value.reason == "token expired"
    assert ws_state.connection_count == 0


def test_ws_from_disallowed_origin_is_closed_4403(ws_state):
    with TestClient(main.app).websocket_connect("/ws/fleet", headers={"Origin": EVIL}) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4403


def test_ws_valid_token_or_no_token_stays_connected_and_always_disconnects(ws_state):
    client = TestClient(main.app)
    for path in (f"/ws/fleet?token={_token(5)}", "/ws/fleet"):  # no token: the currently deployed frontend
        with client.websocket_connect(path, headers={"Origin": ORIGIN}):
            assert ws_state.connection_count == 1
        assert ws_state.connection_count == 0


def test_ws_cartrack_start_failure_sends_status_and_keeps_socket_open(ws_state):
    calls = {"first": 0, "last": 0}

    def failing_start():
        calls["first"] += 1
        raise RuntimeError("scheduler job missing")

    ws_state.on_first_connect = failing_start
    ws_state.on_last_disconnect = lambda: calls.__setitem__("last", calls["last"] + 1)
    with TestClient(main.app).websocket_connect("/ws/fleet") as ws:
        assert ws.receive_json() == {"type": "status", "cartrack": "unavailable"}
        ws.send_text("still here")
        assert ws_state.connection_count == 1  # socket stays open
    assert ws_state.connection_count == 0 and calls == {"first": 1, "last": 0}  # never started, so never paused


def test_ws_poller_start_is_retried_by_the_next_connection(ws_state):
    attempts = []

    def flaky_start():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("first try fails")

    ws_state.on_first_connect = flaky_start
    client = TestClient(main.app)
    with client.websocket_connect("/ws/fleet") as first:
        assert first.receive_json()["cartrack"] == "unavailable"
        with client.websocket_connect("/ws/fleet"):
            assert ws_state._poller_running is True
    assert len(attempts) == 2 and ws_state._poller_running is False


# ---- /health + memory ---------------------------------------------------------------------------------------------------


def test_health_is_async_and_reports_rss(monkeypatch):
    assert inspect.iscoroutinefunction(main.health)
    monkeypatch.setitem(main.schema_state, "ok", True)
    body = TestClient(main.app).get("/health").json()
    assert body["status"] == "ok" and isinstance(body["rss_mb"], float) and body["rss_mb"] > 0


def test_log_rss_is_a_no_op_unless_enabled(monkeypatch, caplog):
    monkeypatch.delenv("MEMORY_DEBUG_LOG", raising=False)
    with caplog.at_level("INFO", logger="memory_debug"):
        memory_debug.log_rss("cache rebuild")
    assert not caplog.records
    monkeypatch.setenv("MEMORY_DEBUG_LOG", "1")
    with caplog.at_level("INFO", logger="memory_debug"):
        memory_debug.log_rss("cache rebuild")
    assert "cache rebuild rss_mb=" in caplog.text
