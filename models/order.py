from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


class Customer(TimestampedBase):
    __tablename__ = "customers"

    name: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[int | None] = mapped_column(BigInteger)
    email: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(200))
    consent_status: Mapped[bool | None] = mapped_column(Boolean)
    # Added per OPTIMIZATION_SCHEMA_MIGRATION.md - geocoded on demand, never re-geocoded once set.
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6))


class Order(TimestampedBase):
    __tablename__ = "orders"

    # Kept as loosely-typed strings (no FK constraint), matching Catalyst's original varchar(200)
    # typing - optimization-data extraction found production customer_id sometimes holds a
    # customer *name* rather than a numeric id, so a strict FK would reject real existing data.
    customer_id: Mapped[str | None] = mapped_column(String(200), index=True)
    route_id: Mapped[str | None] = mapped_column(String(200), index=True)
    vehicle_id: Mapped[str | None] = mapped_column(String(200), index=True)
    status: Mapped[str | None] = mapped_column(Text)
    eta_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eta_window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cargo_temp_c: Mapped[float | None] = mapped_column(Float)
    # Explicit operational inputs; optimization normalizes weight to kilograms.
    shipment_weight: Mapped[float | None] = mapped_column(Numeric(12, 3))
    shipment_weight_unit: Mapped[str | None] = mapped_column(String(16))
    service_time_min: Mapped[float | None] = mapped_column(Numeric(8, 2))


class OrderEvent(TimestampedBase):
    __tablename__ = "order_events"

    order_id: Mapped[str | None] = mapped_column(String(200), index=True)
    event_type: Mapped[str | None] = mapped_column(Text)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class DeliveryProof(TimestampedBase):
    __tablename__ = "delivery_proofs"

    order_id: Mapped[str | None] = mapped_column(String(200), index=True)
    photo_url: Mapped[str | None] = mapped_column(Text)
    signature_ref: Mapped[str | None] = mapped_column(Text)
    recipient_name: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
