"""An uncaught ZohoError (e.g. the budget guard refusing a list call) must reach the browser as a readable 503 that still
carries CORS headers, not as an opaque network error. Mocked: no Zoho, no database."""
from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "test-secret-not-real")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from services import zoho_error_handler
from services.zoho_client import ZohoError

ORIGIN = "https://frontend.example.test"


def make_client(*, handler: bool = True) -> TestClient:
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    if handler:
        zoho_error_handler.register(app)

    @app.get("/guarded")
    def guarded():
        raise ZohoError("Zoho daily budget guard active; non-essential Zoho calls are paused.")

    @app.get("/empty")
    def empty():
        raise ZohoError()

    @app.get("/teapot")
    def teapot():
        raise HTTPException(418, "unchanged")

    return TestClient(app, raise_server_exceptions=False)


def test_zoho_error_becomes_a_readable_503_with_the_real_reason():
    response = make_client().get("/guarded", headers={"Origin": ORIGIN})
    assert response.status_code == 503
    assert response.json() == {"detail": "Zoho daily budget guard active; non-essential Zoho calls are paused.", "code": "zoho_unavailable"}


def test_the_503_keeps_its_cors_headers_so_the_browser_can_read_it():
    response = make_client().get("/guarded", headers={"Origin": ORIGIN})
    assert response.headers["access-control-allow-origin"] == ORIGIN


def test_without_the_handler_the_browser_gets_no_cors_headers_which_is_the_bug_being_fixed():
    response = make_client(handler=False).get("/guarded", headers={"Origin": ORIGIN})
    assert response.status_code == 500 and "access-control-allow-origin" not in response.headers


def test_an_empty_message_still_explains_itself():
    assert make_client().get("/empty", headers={"Origin": ORIGIN}).json()["detail"] == "Zoho is unavailable. Please try again."


def test_other_errors_are_untouched():
    response = make_client().get("/teapot", headers={"Origin": ORIGIN})
    assert response.status_code == 418 and response.json() == {"detail": "unchanged"}
