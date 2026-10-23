from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class ZohoSoLiveCache(Base):
    """Current SO snapshots received from Zoho, separate from assignment history."""

    __tablename__ = "zoho_so_live_cache"

    salesorder_id: Mapped[str] = mapped_column(Text, primary_key=True)
    shipment_date: Mapped[date | None] = mapped_column(Date, index=True)
    branch_id: Mapped[str | None] = mapped_column(Text)
    last_modified_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
