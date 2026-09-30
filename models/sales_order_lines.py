from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


class SalesOrderLine(TimestampedBase):
    """Step 6 (2026-09-24): line items for a persisted (sales_orders) SO, populated once at
    the same time sales_orders itself is written (assign/status-change/delivery) - the slim
    replacement for reading raw_json.line_items. The live/current Zoho path never touches
    this table; it still keeps full raw_json in memory only (services/live_sales_order_cache.py)."""

    __tablename__ = "sales_order_lines"

    sales_order_id: Mapped[str] = mapped_column(String(200), ForeignKey("sales_orders.id", ondelete="CASCADE"), index=True, nullable=False)
    item_id: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str | None] = mapped_column(Text)
    sku: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(Text)
    quantity_shipped: Mapped[float | None] = mapped_column(Float)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    location_name: Mapped[str | None] = mapped_column(Text)
