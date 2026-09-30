from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, get_current_user
from auth.security import create_access_token, hash_password, verify_password
from database import get_db
from models.user import User
from services import user_cache

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginBody(BaseModel):
    email: str
    password: str


def _public_user(user: User) -> dict:
    return {"id": user.id, "fullName": user.full_name, "email": user.email, "role": user.role, "status": user.status, "mustChangePassword": user.must_change_password}


def _public_user_from_current(current_user: CurrentUser) -> dict:
    return {"id": current_user.id, "fullName": current_user.full_name, "email": current_user.email, "role": current_user.role, "status": current_user.status, "mustChangePassword": current_user.must_change_password}


@router.post("/login")
def login(body: LoginBody, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    password = body.password

    if not email or not password:
        raise HTTPException(400, "email and password are required")

    user = db.execute(select(User).where(func.lower(User.email) == email)).scalar_one_or_none()

    # Same response whether the email doesn't exist or the password doesn't match - never
    # reveal which case it was (ported verbatim from auth-module/index.js).
    if user is None or not user.password_hash or not verify_password(password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")

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
    if len(body.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    db.commit()
    user_cache.invalidate(user.id)
    return {"ok": True}


@router.post("/logout")
def logout():
    return {"ok": True}


@router.get("/session")
def get_session(current_user: CurrentUser = Depends(get_current_user)):
    # current_user already came through the 10-min user_cache (auth/dependencies.py) - no
    # fresh DB read here on a cache hit.
    return {"user": _public_user_from_current(current_user)}
