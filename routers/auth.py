from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, get_current_user
from auth.security import create_access_token, hash_password, password_problem, verify_password
from database import get_db
from models.user import User
from services import login_rate_limiter, user_cache, user_invites
from services.phone_numbers import ph_phone_lookup_values

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("auth")

GENERIC_LOGIN_ERROR = "Invalid credentials"
RATE_LIMITED_ERROR = "Too many attempts. Please try again later."
INVALID_LINK_ERROR = "This link is invalid or has expired. Ask dispatch for a new one."
# bcrypt hash of a throwaway value, checked when the account does not exist so "unknown account" and "wrong
# password" take the same time.
_DUMMY_HASH = hash_password("not-a-real-password")


class LoginBody(BaseModel):
    # `identifier` is an email address or a PH mobile number (09xxxxxxxxx / +639xxxxxxxxx / 639xxxxxxxxx).
    # `email` is the pre-driver-portal field name, still accepted.
    identifier: str | None = None
    email: str | None = None
    password: str


def _public_user(user: User) -> dict:
    return {"id": user.id, "fullName": user.full_name, "email": user.email, "role": user.role, "status": user.status, "mustChangePassword": user.must_change_password, "staffId": user.staff_id}


def _public_user_from_current(current_user: CurrentUser) -> dict:
    return {"id": current_user.id, "fullName": current_user.full_name, "email": current_user.email, "role": current_user.role, "status": current_user.status, "mustChangePassword": current_user.must_change_password, "staffId": current_user.staff_id}


def client_ip(request: Request) -> str:
    """Caller IP for rate limiting. Behind the Render proxy the original client is the first X-Forwarded-For entry
    (a client can forge that entry to dodge the per-IP limit, but the per-account limit still binds)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    return first or (request.client.host if request.client else "unknown")


def _account_key(identifier: str) -> str:
    phones = ph_phone_lookup_values(identifier)
    return f"acct:{phones[0]}" if phones else f"acct:{identifier.strip().lower()}"


def _find_user(db: Session, identifier: str) -> User | None:
    if "@" in identifier:
        return db.execute(select(User).where(func.lower(User.email) == identifier.strip().lower())).scalar_one_or_none()
    phones = ph_phone_lookup_values(identifier)
    if not phones:
        return None
    return db.execute(select(User).where(or_(*[User.phone == value for value in phones]))).scalars().first()


@router.post("/login")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    identifier = (body.identifier or body.email or "").strip()
    password = body.password

    if not identifier or not password:
        raise HTTPException(400, "email or phone, and password, are required")

    account_key, ip_key = _account_key(identifier), f"ip:{client_ip(request)}"
    user = _find_user(db, identifier)
    # Throttle keys: what was typed, the caller's IP, and - for a real account - the account itself, so switching
    # between the email and the phone form of the same login does not buy a fresh set of attempts.
    keys = [account_key, ip_key] + ([f"uid:{user.id}"] if user is not None else [])
    if login_rate_limiter.is_blocked(*keys):
        raise HTTPException(429, RATE_LIMITED_ERROR, headers={"Retry-After": str(login_rate_limiter.retry_after_seconds(*keys))})

    # Same response whether the account doesn't exist or the password doesn't match - never
    # reveal which case it was.
    if user is None or not user.password_hash:
        verify_password(password, _DUMMY_HASH)
        login_rate_limiter.record_failure(*keys)
        raise HTTPException(401, GENERIC_LOGIN_ERROR)
    if not verify_password(password, user.password_hash):
        login_rate_limiter.record_failure(*keys)
        raise HTTPException(401, GENERIC_LOGIN_ERROR)
    # Only someone who knows the password learns the account is disabled.
    if user.status not in (None, "active"):
        raise HTTPException(403, "This account is disabled. Please contact dispatch.")

    login_rate_limiter.clear(account_key, f"uid:{user.id}")
    token = create_access_token(user_id=user.id, role=user.role, email=user.email)
    return {"user": _public_user(user), "token": token}


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
def change_password(body: ChangePasswordBody, current_user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    user = db.get(User, current_user.id)
    if user is None:
        raise HTTPException(401, "Session invalid or expired")
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(401, "Current password is incorrect")
    problem = password_problem(body.new_password)
    if problem:
        raise HTTPException(400, problem)
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    db.commit()
    user_cache.invalidate(user.id)
    return {"ok": True}


class SetPasswordBody(BaseModel):
    token: str
    password: str


@router.post("/set-password")
def set_password(body: SetPasswordBody, request: Request, db: Session = Depends(get_db)):
    """Public: redeem an invite / reset link. The token is single-use and expires after 24 hours; every failure
    gives the same message so a guessed token reveals nothing."""
    ip_key = f"ip-setpw:{client_ip(request)}"
    if login_rate_limiter.is_blocked(ip_key):
        raise HTTPException(429, RATE_LIMITED_ERROR)
    problem = password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)
    user = user_invites.redeem(db, body.token.strip())
    if user is None or user.status not in (None, "active"):
        db.rollback()
        login_rate_limiter.record_failure(ip_key)
        raise HTTPException(400, INVALID_LINK_ERROR)
    user.password_hash = hash_password(body.password)
    user.must_change_password = False
    db.commit()
    user_cache.invalidate(user.id)
    logger.info("[AUTH] password set via link for user_id=%s", user.id)
    return {"ok": True}


@router.post("/logout")
def logout():
    # JWTs are stateless: the client discards its token. (A disabled account is cut off by the status check.)
    return {"ok": True}


@router.get("/session")
def get_session(current_user: CurrentUser = Depends(get_current_user)):
    # current_user already came through the 10-min user_cache (auth/dependencies.py) - no
    # fresh DB read here on a cache hit.
    return {"user": _public_user_from_current(current_user)}
