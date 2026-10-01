"""Synchronous GET sharing. See docs/ZOHO_ACQUISITION.md for freshness rules."""
from __future__ import annotations

from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import logging
from threading import RLock
from uuid import uuid4

from services.zoho_rate_limiter import metrics

logger = logging.getLogger("zoho")
_lock = RLock()
_generation = 0
_inflight: dict[tuple, Future] = {}
_scope: ContextVar[dict | None] = ContextVar("zoho_acquisition_scope", default=None)
_source: ContextVar[str] = ContextVar("zoho_source", default="unspecified")
_request: ContextVar[str] = ContextVar("zoho_route_request", default="background")
_route: ContextVar[str] = ContextVar("zoho_route", default="background")


_COUNTED = {"logical_request", "cache_hit", "cache_miss", "coalesced_waiter", "http_success", "http_429", "http_retry"}


def event(kind: str, **fields) -> None:
    if kind in _COUNTED:
        metrics.record(kind)
    # Callers pass only diagnostic metadata, never headers or response bodies.
    logger.info("[ZOHO_ACQUIRE] event=%s source=%s route=%s request_id=%s %s", kind, _source.get(), _route.get(), _request.get(),
                " ".join(f"{key}={str(value).replace(chr(32), chr(95))}" for key, value in fields.items()))


def generation() -> int:
    with _lock:
        return _generation


@contextmanager
def publication(expected: int):
    """Atomic generation check + memory publication; never put I/O in this block."""
    with _lock:
        yield expected == _generation


@contextmanager
def invalidation():
    """Advance the barrier and clear the caller's existing cache atomically."""
    global _generation
    with _lock:
        _generation += 1
        yield


@contextmanager
def source(name: str):
    token = _source.set(name)
    try:
        yield
    finally:
        _source.reset(token)


@contextmanager
def request_context(path: str):
    route_token = _route.set(path)
    request_token = _request.set(uuid4().hex)
    try:
        yield
    finally:
        _route.reset(route_token)
        _request.reset(request_token)


def operation(name: str, *, reuse_details: bool = False):
    """An explicit freshness domain, optionally reusing successful SO details.

    Completed data lives only for this invocation, including copied thread contexts.
    Independent invocations never reuse completed results, even milliseconds apart.
    """
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            token = _scope.set({"semantics": name, "completed": {}, "reuse": reuse_details})
            # Background runs (scheduler) get their own id so one run's calls can be told apart in logs.
            request_token = _request.set(uuid4().hex) if _request.get() == "background" else None
            try:
                with source(name):
                    return fn(*args, **kwargs)
            finally:
                if request_token is not None:
                    _request.reset(request_token)
                _scope.reset(token)
        return wrapped
    return decorate


def acquire(key: tuple, loader, *, reusable: bool = False, valid=lambda value: True, meta: dict | None = None):
    """Share only equivalent in-flight reads. Failures are never retained."""
    scope = _scope.get()
    logical_id = uuid4().hex
    meta = meta or {}  # diagnostics only (method/endpoint); never part of the key
    with _lock:
        epoch = _generation
        full_key = (epoch, scope["semantics"] if scope else "live", key)
        completed = scope["completed"] if scope and scope["reuse"] and reusable else None
        event("logical_request", logical_id=logical_id, generation=epoch, **meta)
        if completed is not None and full_key in completed:
            event("cache_hit", logical_id=logical_id, generation=epoch, **meta)
            return deepcopy(completed[full_key])
        future = _inflight.get(full_key)
        owner = future is None
        if owner:
            future = Future()
            future.acquisition_id = logical_id
            _inflight[full_key] = future
        event("cache_miss" if owner else "coalesced_waiter", logical_id=logical_id, owner_id=future.acquisition_id, generation=epoch, **meta)
    if not owner:
        value = future.result()
        with _lock:
            if completed is not None and epoch == _generation and valid(value):
                completed[full_key] = deepcopy(value)
        return deepcopy(value)
    try:
        value = loader(logical_id)
        with _lock:
            if completed is not None and epoch == _generation and valid(value):
                completed[full_key] = deepcopy(value)
        future.set_result(deepcopy(value))
        return value
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _lock:
            if _inflight.get(full_key) is future:
                del _inflight[full_key]
