from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Text
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
    # Links a login to its record in the n8n staff directory (plain int, no FK: staff are not in Neon).
    # Migration n4f5a6b7c8d9. Driver endpoints scope every query by this value.
    staff_id: Mapped[int | None] = mapped_column(Integer, unique=True, index=True)



class UserInvite(TimestampedBase):
    """Single-use, expiring set-password link (migration n4f5a6b7c8d9). Only the SHA-256 of the token is
    stored; the token itself exists only in the link that is emailed / copied by an admin."""

    __tablename__ = "user_invites"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)  # "invite" | "reset"
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Role(TimestampedBase):
    __tablename__ = "roles"

    name: Mapped[str | None] = mapped_column(Text)
    permissions_json: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)



# Session, AuthEvent, and TenantScope were dropped 2026-09-24 (migration
# d4a1c8f6e9b2_drop_dead_comms_and_session_tables) - confirmed zero read/write sites
# anywhere in the app. JWT auth is stateless (no session row needed); login/logout no
# longer write an audit row (auth.py); no code ever referenced tenant scoping.
