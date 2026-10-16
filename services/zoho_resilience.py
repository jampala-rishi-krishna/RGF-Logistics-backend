"""Small helpers that keep Zoho-backed request paths answering when Zoho is slow or failing. Memory only: nothing here reads or
writes the database.

* SingleFlight: one upstream pull at a time per cache; concurrent callers share it, and a request waits at most a deadline for it
  (the pull keeps running in the background and fills the cache for the next request).
* light_error: a remembered failure without its traceback (a stored traceback pins every frame - and the payloads in them - in memory).
* is_budget_error: the Zoho budget guard refusing a call (a ZohoError raised from ZohoBudgetGuard).
* track_stale / note_stale: a request-scoped flag set when a route answered from the last good data instead of fresh Zoho data.
"""
from __future__ import annotations

import contextvars
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import contextmanager
from datetime import datetime, timezone

# A request never waits longer than this for a Zoho pull before answering from memory (or 503 "loading").
REQUEST_DEADLINE_SECONDS = 20.0


class DeadlineExceeded(Exception):
    """The shared pull is still running after the request's deadline; it keeps running in the background."""


def is_budget_error(exc: BaseException | None) -> bool:
    """True when Zoho was not called because the daily budget guard refused it."""
    seen = 0
    while exc is not None and seen < 5:
        if getattr(exc, "zoho_budget", False) or type(exc).__name__ == "ZohoBudgetGuard":
            return True
        if "budget" in str(exc).lower():
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def light_error(exc: BaseException) -> BaseException:
    """A copy of `exc` (same class and message where possible) with no traceback, cause or context."""
    try:
        light = type(exc)(str(exc))
    except Exception:  # noqa: BLE001 - an exception class with a custom constructor
        light = RuntimeError(f"{type(exc).__name__}: {exc}")
    if is_budget_error(exc):
        try:
            light.zoho_budget = True
        except Exception:  # noqa: BLE001
            pass
    light.__traceback__ = None
    light.__cause__ = None
    light.__context__ = None
    light.__suppress_context__ = True
    return light


class SingleFlight:
    """At most one `work` running at a time. Callers that arrive while it runs share its result (or its error)."""

    def __init__(self, name: str):
        self.name = name
        self._lock = threading.Lock()
        self._future: Future | None = None

    def running(self) -> bool:
        with self._lock:
            return self._future is not None and not self._future.done()

    def run(self, work, *, timeout: float | None = REQUEST_DEADLINE_SECONDS):
        """Start `work` on a daemon thread unless one is already running, then wait at most `timeout` seconds for it.
        Returns its result, re-raises its (traceback-free) error, or raises DeadlineExceeded while it keeps running."""
        with self._lock:
            future = self._future
            owner = future is None or future.done()
            if owner:
                future = Future()
                self._future = future
        if owner:
            thread = threading.Thread(target=contextvars.copy_context().run, args=(self._execute, future, work), name=f"flight-{self.name}", daemon=True)
            thread.start()
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            raise DeadlineExceeded(f"{self.name} is still loading from Zoho") from None

    @staticmethod
    def _execute(future: Future, work) -> None:
        try:
            result = work()
        except BaseException as exc:  # noqa: BLE001 - handed to every waiter
            future.set_exception(light_error(exc))
        else:
            future.set_result(result)


# ---- request-scoped "answered from stale data" flag --------------------------------------------------------------------

_stale: contextvars.ContextVar[dict | None] = contextvars.ContextVar("zoho_stale_marker", default=None)


@contextmanager
def track_stale():
    """Route helper: `with track_stale() as stale:` ... then stale["stale"] / stale["stale_since"] say whether any cache answered from
    its last good data because Zoho failed or was too slow."""
    marker = {"stale": False, "stale_since": None, "reason": None}
    token = _stale.set(marker)
    try:
        yield marker
    finally:
        _stale.reset(token)


def note_stale(since: datetime | None = None, reason: str | None = None) -> None:
    marker = _stale.get()
    if marker is None:
        return
    marker["stale"] = True
    if since is not None:
        since_iso = since.astimezone(timezone.utc).isoformat() if since.tzinfo else since.replace(tzinfo=timezone.utc).isoformat()
        if marker["stale_since"] is None or since_iso < marker["stale_since"]:
            marker["stale_since"] = since_iso
    if reason and not marker["reason"]:
        marker["reason"] = reason


def stale_fields(marker: dict) -> dict:
    """The JSON fields a list response carries when it was answered from stale data (nothing when it is fresh)."""
    if not marker.get("stale"):
        return {}
    return {"stale": True, "stale_since": marker.get("stale_since")}


ZOHO_DOWN_MESSAGE = "Zoho is not responding, showing last saved data"


def unavailable_body(exc: BaseException | None) -> dict:
    """503 body when Zoho failed and there is no data in memory to show."""
    if exc is not None and is_budget_error(exc):
        return {"detail": str(exc) or "Zoho daily budget guard active; Zoho calls are paused.", "code": "zoho_budget", "stale": True}
    return {"detail": ZOHO_DOWN_MESSAGE, "code": "zoho_unavailable", "stale": True}


WARMING_BODY = {"detail": "Loading from Zoho. Try again in a few seconds.", "code": "warming", "stale": True}
