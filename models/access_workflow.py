"""Durable decisions and send outcomes; live Zoho content stays in memory."""
from datetime import datetime, date
from sqlalchemy import Boolean, Date, DateTime, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from database import TimestampedBase


class UnassignRequest(TimestampedBase):
    __tablename__ = "unassign_requests"
    __table_args__ = (Index("uq_unassign_pending_order", "order_id", unique=True,
                           postgresql_where=text("status='pending'"), sqlite_where=text("status='pending'")),)
    order_id: Mapped[str] = mapped_column(Text, nullable=False)
    order_number: Mapped[str] = mapped_column(Text, nullable=False)
    requester_id: Mapped[int] = mapped_column(Integer, nullable=False)
    requester_name: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    resolved_by: Mapped[int | None] = mapped_column(Integer)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    manager_reads: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    result_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PickupDecision(TimestampedBase):
    __tablename__ = "pickup_decisions"
    decision_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    order_id: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[int] = mapped_column(Integer, nullable=False)
    decided_by_name: Mapped[str] = mapped_column(Text, nullable=False)


class PickupEmailLog(TimestampedBase):
    __tablename__ = "pickup_email_logs"
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False)
    recipients: Mapped[dict] = mapped_column(JSONB, nullable=False)
    po_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    # Unique for the scheduled daily attempt; NULL on manual sends.
    schedule_key: Mapped[str | None] = mapped_column(Text, unique=True)
