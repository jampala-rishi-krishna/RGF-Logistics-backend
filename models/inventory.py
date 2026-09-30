from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


class SalesOrderCache(TimestampedBase):
    __tablename__ = "sales_orders_cache"

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    salesorder_number: Mapped[str | None] = mapped_column(Text)
    reference_number: Mapped[str | None] = mapped_column(Text)
    customer_name: Mapped[str | None] = mapped_column(Text)
    order_status: Mapped[str | None] = mapped_column(Text)
    invoice_status: Mapped[str | None] = mapped_column(Text)
    payment_status: Mapped[str | None] = mapped_column(Text)
    shipment_status: Mapped[str | None] = mapped_column(Text)
    order_date: Mapped[date | None] = mapped_column(Date)
    expected_shipment_date: Mapped[date | None] = mapped_column(Date)
    total: Mapped[float | None] = mapped_column(Numeric(14, 2))
    delivery_method: Mapped[str | None] = mapped_column(Text)
    salesperson_name: Mapped[str | None] = mapped_column(Text)
    customer_po_number: Mapped[str | None] = mapped_column(Text)
    billing_address: Mapped[dict | None] = mapped_column(JSONB)
    shipping_address: Mapped[dict | None] = mapped_column(JSONB)
    payment_terms_label: Mapped[str | None] = mapped_column(Text)
    mode_of_transport: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    vehicle_id: Mapped[str | None] = mapped_column(String(200), index=True)
    driver_id: Mapped[int | None] = mapped_column(Integer, index=True)
    route_id: Mapped[str | None] = mapped_column(String(200), index=True)
    manifest_id: Mapped[int | None] = mapped_column(Integer, index=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_by: Mapped[int | None] = mapped_column(Integer)
    assignment_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unassigned", index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
