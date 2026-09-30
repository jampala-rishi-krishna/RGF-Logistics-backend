from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


class SalesOrderHistory(TimestampedBase):
    """Step 6 (2026-09-24): table renamed sales_order_history -> sales_orders, slimmed -
    raw_json dropped. Line items moved to sales_order_lines (models/sales_order_lines.py).
    delivery_status/is_reefer are now computed once at write time (see
    services/sales_order_history_sync.py) instead of being derived from raw_json on every
    read. A row here is written ONLY at assign/status-change/delivery, never a bulk Zoho
    mirror - unassigned/current-date SOs are never written here at all; those stay
    live-from-Zoho + services/live_sales_order_cache.py's in-memory cache (which still keeps
    full raw_json, in memory only, never persisted - Confirmed SO's current/future view and
    per-line weight breakdown for TODAY still work exactly as before this change).
    """

    __tablename__ = "sales_orders"

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    salesorder_number: Mapped[str | None] = mapped_column(Text)
    reference_number: Mapped[str | None] = mapped_column(Text)
    customer_name: Mapped[str | None] = mapped_column(Text)
    order_status: Mapped[str | None] = mapped_column(Text)
    invoice_status: Mapped[str | None] = mapped_column(Text)
    payment_status: Mapped[str | None] = mapped_column(Text)
    shipment_status: Mapped[str | None] = mapped_column(Text)
    delivery_status: Mapped[str | None] = mapped_column(Text)
    is_reefer: Mapped[bool | None] = mapped_column(Boolean)
    mets_qty_available_for_sale: Mapped[float | None] = mapped_column(Numeric(14, 3))
    glacier_qty_available_for_sale: Mapped[float | None] = mapped_column(Numeric(14, 3))
    order_date: Mapped[date | None] = mapped_column(Date)
    expected_shipment_date: Mapped[date | None] = mapped_column(Date, index=True)
    total: Mapped[float | None] = mapped_column(Numeric(14, 2))
    delivery_method: Mapped[str | None] = mapped_column(Text)
    salesperson_name: Mapped[str | None] = mapped_column(Text)
    customer_po_number: Mapped[str | None] = mapped_column(Text)
    billing_address: Mapped[dict | None] = mapped_column(JSONB)
    shipping_address: Mapped[dict | None] = mapped_column(JSONB)
    payment_terms_label: Mapped[str | None] = mapped_column(Text)
    mode_of_transport: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    vehicle_id: Mapped[str | None] = mapped_column(String(200), index=True)
    driver_id: Mapped[int | None] = mapped_column(Integer, index=True)
    route_id: Mapped[str | None] = mapped_column(String(200), index=True)
    manifest_id: Mapped[int | None] = mapped_column(Integer, index=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_by: Mapped[int | None] = mapped_column(Integer)
    assignment_status: Mapped[str] = mapped_column(String(24), nullable=False, default="assigned", index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    helper_ids: Mapped[list | None] = mapped_column(JSONB)
