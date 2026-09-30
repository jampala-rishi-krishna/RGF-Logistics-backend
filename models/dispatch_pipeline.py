from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


class VehicleCapacityProfile(TimestampedBase):
    __tablename__ = "vehicle_capacity_profiles"

    vehicle_type: Mapped[str] = mapped_column(Text, nullable=False)
    plate_no: Mapped[str | None] = mapped_column(String(200), unique=True)
    rated_capacity_kg: Mapped[float | None] = mapped_column(Float)
    capacity_note: Mapped[str | None] = mapped_column(Text)
    is_reefer: Mapped[bool | None] = mapped_column(Boolean)
    is_gps_tracked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_third_party: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    driver_id: Mapped[int | None] = mapped_column(Integer, index=True)


class ClientDeliveryConstraint(TimestampedBase):
    __tablename__ = "client_delivery_constraints"

    customer_name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    opening_time: Mapped[str | None] = mapped_column(String(16))
    receiving_cutoff_time: Mapped[str | None] = mapped_column(String(16))
    avg_processing_time_minutes: Mapped[float | None] = mapped_column(Float)
    requires_reefer: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)


class WarehouseLoadingChecklist(TimestampedBase):
    __tablename__ = "warehouse_loading_checklists"

    manifest_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    seal_number: Mapped[str | None] = mapped_column(String(200))
    cargo_count_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cargo_count_expected: Mapped[int | None] = mapped_column(Integer)
    cargo_count_actual: Mapped[int | None] = mapped_column(Integer)
    departure_temp_c: Mapped[float | None] = mapped_column(Float)
    departure_temp_zone_count: Mapped[int | None] = mapped_column(Integer)
    driver_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    checklist_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by: Mapped[int | None] = mapped_column(Integer)
