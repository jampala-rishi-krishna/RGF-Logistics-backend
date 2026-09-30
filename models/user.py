from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


class User(TimestampedBase):
    __tablename__ = "users"

    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    phone: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    role: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    profile_photo_url: Mapped[str | None] = mapped_column(Text)
    # Not present in the original schema doc (Catalyst introspection missed it) but read/written
    # by auth-module's login handler - required for bcrypt-based auth.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Forces a change-password flow on next login. Set True by scripts/seed_minimal.py for the
    # admin account (its password was printed in plaintext to stdout/logs at seed time).
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Role(TimestampedBase):
    __tablename__ = "roles"

    name: Mapped[str | None] = mapped_column(Text)
    permissions_json: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)



# Session, AuthEvent, and TenantScope were dropped 2026-09-24 (migration
# d4a1c8f6e9b2_drop_dead_comms_and_session_tables) - confirmed zero read/write sites
# anywhere in the app. JWT auth is stateless (no session row needed); login/logout no
# longer write an audit row (auth.py); no code ever referenced tenant scoping.
