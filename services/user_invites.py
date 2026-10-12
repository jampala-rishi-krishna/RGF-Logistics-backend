"""Single-use, expiring set-password / reset links (table user_invites, migration n4f5a6b7c8d9).

Only the SHA-256 of the token is stored. The token itself exists only inside the link the admin copies or the email
that is sent, and is never logged. Writes happen on events only: an invite is created, an invite is redeemed.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from models.user import User, UserInvite

INVITE_TTL = timedelta(hours=24)
PURPOSES = ("invite", "reset")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def create_invite(db: Session, user: User, purpose: str, *, now: datetime | None = None) -> tuple[str, datetime]:
    """New link for `user`; any still-pending earlier link for the same user is revoked. Caller commits."""
    if purpose not in PURPOSES:
        raise ValueError("purpose must be 'invite' or 'reset'")
    now = now or _now()
    db.execute(update(UserInvite).where(UserInvite.user_id == user.id, UserInvite.used_at.is_(None), UserInvite.expires_at > now).values(expires_at=now).execution_options(synchronize_session=False))
    token = secrets.token_urlsafe(32)
    expires_at = now + INVITE_TTL
    db.add(UserInvite(user_id=user.id, token_hash=hash_token(token), purpose=purpose, expires_at=expires_at))
    return token, expires_at


def redeem(db: Session, token: str, *, now: datetime | None = None) -> User | None:
    """Atomically consume `token` and return its user, or None when it is unknown, expired or already used.
    The consuming UPDATE only matches an unused, unexpired row, so two concurrent redemptions cannot both win.
    Caller sets the new password and commits."""
    if not token or len(token) > 200:
        return None
    now = now or _now()
    invite = db.execute(select(UserInvite).where(UserInvite.token_hash == hash_token(token))).scalar_one_or_none()
    if invite is None:
        return None
    result = db.execute(update(UserInvite).where(UserInvite.id == invite.id, UserInvite.used_at.is_(None), UserInvite.expires_at > now).values(used_at=now).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        return None
    return db.get(User, invite.user_id)


def invite_states(db: Session, user_ids: list[int], *, now: datetime | None = None) -> dict[int, str]:
    """'none' | 'pending' | 'expired' | 'used' per user, from each user's latest invite. One query."""
    now = now or _now()
    states = {uid: "none" for uid in user_ids}
    if not user_ids:
        return states
    rows = db.execute(select(UserInvite).where(UserInvite.user_id.in_(user_ids)).order_by(UserInvite.id)).scalars().all()
    for invite in rows:  # ascending id: the last row per user wins
        if invite.used_at is not None:
            states[invite.user_id] = "used"
        elif _aware(invite.expires_at) <= now:
            states[invite.user_id] = "expired"
        else:
            states[invite.user_id] = "pending"
    return states
