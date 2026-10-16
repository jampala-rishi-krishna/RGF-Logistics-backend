from __future__ import annotations

from dataclasses import asdict, dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth.security import decode_access_token
from database import SessionLocal
from models.user import User
from services import user_cache

bearer_scheme = HTTPBearer(auto_error=False)

# A planner works the same operational pages as a dispatcher (Control Tower, Fleet, Load
# Planning, Orders, ...). Admin-only pages stay admin-only. The user's stored role remains
# "planner"; only the permission check treats it as a dispatcher.
ROLE_ALIASES = {"planner": "dispatcher"}


@dataclass
class CurrentUser:
    id: int
    email: str
    role: str | None
    full_name: str
    status: str | None
    must_change_password: bool = False
    staff_id: int | None = None
    is_manager: bool = False


def _load_user(user_id: int) -> CurrentUser | None:
    """Loaded once per TTL window (services/user_cache.py), not per request. A normal
    authenticated request never opens a DB session at all on a cache hit.

    Disabling a user: the admin status endpoint invalidates this cache entry (immediate on the single API
    instance). Worst case, e.g. a status changed directly in the database, the user is cut off when the entry
    expires - at most user_cache.TTL_SECONDS (10 minutes) - even though a driver JWT lives 30 days."""
    cached = user_cache.get(user_id)
    if cached is not None:
        return CurrentUser(**cached)
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None or user.status not in (None, "active"):
            return None
        current = CurrentUser(id=user.id, email=user.email, role=user.role, full_name=user.full_name, status=user.status, must_change_password=user.must_change_password, staff_id=user.staff_id, is_manager=user.is_manager)
    user_cache.put(user_id, asdict(current))
    return current


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> CurrentUser:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = _load_user(int(payload["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
    return user


def get_current_user_optional(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> CurrentUser | None:
    if credentials is None:
        return None
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        return None
    return _load_user(int(payload["sub"]))


def require_role(*allowed_roles: str):
    """Replaces the Catalyst requireRole(app, allowedRoles) pattern. Unlike the original, this
    does NOT fall back to trusting a spoofable X-User-Role header when there's no session -
    that demo-mode fallback was explicitly documented as insecure and is superseded by real
    JWT auth here; a missing/invalid token is now always a hard 401/403."""

    def dependency(current_user: CurrentUser = Depends(get_current_user), request: Request = None) -> CurrentUser:
        # Sales / Wet Market may read order lists and details only, never mutate them.
        readonly = current_user.role in {"sales", "wet_market"} and request is not None and request.method == "GET" and (
            request.url.path.startswith("/orders") or request.url.path.startswith("/api/orders") or
            request.url.path.startswith("/api/load-planning/inventory/sales-orders") or
            request.url.path.startswith("/api/load-planning/inventory/export/") or
            request.url.path == "/api/load-planning/inventory/branches")
        if not readonly and ROLE_ALIASES.get(current_user.role, current_user.role) not in allowed_roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden")
        return current_user

    return dependency


def require_manager(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not current_user.is_manager or current_user.role not in {"admin", "planner", "dispatcher"}:
        raise HTTPException(403, "Manager permission required")
    return current_user


def workspace_access(request: Request, user: CurrentUser | None = Depends(get_current_user_optional)):
    if user is None:
        return
    path = request.url.path
    if user.role in {"sales", "wet_market"}:
        allowed = path.startswith("/auth/") or path.startswith("/api/access/") or (request.method == "GET" and (
            path.startswith("/orders") or path.startswith("/api/orders") or
            path.startswith("/api/load-planning/inventory/sales-orders") or
            path.startswith("/api/load-planning/inventory/export/") or path == "/api/load-planning/inventory/branches"))
        if not allowed:
            raise HTTPException(403, "Orders read-only access")
    if user.role in {"planner", "dispatcher"} and (path.startswith("/api/fleet-health") or
            path.startswith("/api/admin") or path.startswith("/api/officer") or
            path in {"/users", "/roles", "/audit-log", "/integrations", "/warehouse-events"}):
        raise HTTPException(403, "Administrator access required")
