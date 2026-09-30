from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


class Alert(TimestampedBase):
    __tablename__ = "alerts"

    type: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str | None] = mapped_column(Text)
    vehicle_id: Mapped[str | None] = mapped_column(String(200), index=True)
    order_id: Mapped[str | None] = mapped_column(String(200), index=True)
    message: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertEscalation(TimestampedBase):
    __tablename__ = "alert_escalations"

    alert_id: Mapped[str | None] = mapped_column(String(200), index=True)
    escalated_to_user_id: Mapped[str | None] = mapped_column(String(200))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)
