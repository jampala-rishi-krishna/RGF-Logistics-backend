from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
import bcrypt

JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"
# JWT lifetime is a maximum lifetime. The five-minute idle policy is enforced by
# the frontend activity timer; active users must not be logged out at five minutes.
JWT_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "480"))
# Drivers use the app on a phone all day / all week: 30 days by default. A disabled driver is still cut off within
# user_cache.TTL_SECONDS (10 min) because every request re-checks users.status, not just the token.
JWT_DRIVER_EXPIRE_MINUTES = int(os.environ.get("JWT_DRIVER_EXPIRE_MINUTES", str(30 * 24 * 60)))

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_BYTES = 72  # bcrypt ignores / rejects anything longer


def password_problem(password: str) -> str | None:
    """None when acceptable, else a message for the user."""
    if len(password or "") < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        return f"Password must be at most {PASSWORD_MAX_BYTES} bytes."
    return None

def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.strip().encode("utf-8"))
    except ValueError:  # over-long password (bcrypt's 72-byte limit) or a malformed stored hash: never a match
        return False


def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def unusable_password_hash() -> str:
    """A bcrypt hash of a random secret that is never stored or shown: nobody can log in with it until a
    set-password link replaces it."""
    import secrets

    return hash_password(secrets.token_urlsafe(32))


def create_access_token(*, user_id: int, role: str | None, email: str) -> str:
    minutes = JWT_DRIVER_EXPIRE_MINUTES if str(role or "").lower() in ("driver", "helper") else JWT_EXPIRE_MINUTES
    expire = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    payload = {"sub": str(user_id), "role": role, "email": email, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
