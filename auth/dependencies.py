from __future__ import annotations

from dataclasses import asdict, dataclass

from fastapi import Depends, HTTPException, status
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


def _load_user(user_id: int) -> CurrentUser | None:
    """Loaded once per TTL window (services/user_cache.py), not per request. A normal
    authenticated request never opens a DB session at all on a cache hit."""
    cached = user_cache.get(user_id)
    if cached is not None:
        return CurrentUser(**cached)
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None or user.status not in (None, "active"):
            return None
        current = CurrentUser(id=user.id, email=user.email, role=user.role, full_name=user.full_name, status=user.status, must_change_password=user.must_change_password)
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

    def dependency(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if ROLE_ALIASES.get(current_user.role, current_user.role) not in allowed_roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden")
        return current_user

    return dependency
