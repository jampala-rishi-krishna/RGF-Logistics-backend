from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


class AuditLog(TimestampedBase):
    __tablename__ = "audit_log"

    # Mixed identity source in the original (a Catalyst platform email OR a client-supplied
    # numeric actor id) - kept as free text rather than a FK, matching current behavior exactly.
    actor_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    target_entity: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details_json: Mapped[str | None] = mapped_column(Text)


class IntegrationStatus(TimestampedBase):
    __tablename__ = "integration_status"

    provider: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
