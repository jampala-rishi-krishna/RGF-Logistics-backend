from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


class Warehouse(TimestampedBase):
    # Catalyst's live table was named "Warehouse" (capital, singular) - normalized here.
    __tablename__ = "warehouse"

    name: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    zone: Mapped[str | None] = mapped_column(Text)


class WarehouseEvent(TimestampedBase):
    __tablename__ = "warehouse_events"

    warehouse_id: Mapped[str | None] = mapped_column(String(200), index=True)
    event_type: Mapped[str | None] = mapped_column(Text)
    manifest_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str | None] = mapped_column(Text)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
