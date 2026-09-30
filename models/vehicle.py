from __future__ import annotations

from sqlalchemy import Boolean, Float, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


class Driver(TimestampedBase):
    __tablename__ = "drivers"

    user_id: Mapped[str | None] = mapped_column(String(200))
    license_no: Mapped[str | None] = mapped_column(Text)
    preferred_channel: Mapped[str | None] = mapped_column(Text)
    current_vehicle_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str | None] = mapped_column(Text)


class Vehicle(TimestampedBase):
    """Static roster data only. Live telemetry (position, heading, speed, fuel,
    ignition, status, zone) lives exclusively in services.live_gps_store, in
    memory - never in this table. See migration a3d5f8c1b2e4."""

    __tablename__ = "vehicles"

    plate_no: Mapped[str | None] = mapped_column(String(200))
    driver_id: Mapped[str | None] = mapped_column(String(200))
    vehicle_type: Mapped[str | None] = mapped_column(Text)
    rated_capacity_kg: Mapped[float | None] = mapped_column(Float)
    capacity_note: Mapped[str | None] = mapped_column(Text)
    is_reefer: Mapped[bool | None] = mapped_column(Boolean)
    is_gps_tracked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_third_party: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    capacity_kg: Mapped[float | None] = mapped_column(Numeric(10, 2))
    capacity_m3: Mapped[float | None] = mapped_column(Numeric(10, 2))
    temperature_capability: Mapped[str | None] = mapped_column(Text)
    shift_start: Mapped[str | None] = mapped_column(Text)
    shift_end: Mapped[str | None] = mapped_column(Text)
    cost_per_km: Mapped[float | None] = mapped_column(Numeric(10, 2))
    cost_per_hour: Mapped[float | None] = mapped_column(Numeric(10, 2))
    depot_lat: Mapped[float | None] = mapped_column(Numeric(9, 6))
    depot_lng: Mapped[float | None] = mapped_column(Numeric(9, 6))


class Geofence(TimestampedBase):
    __tablename__ = "geofences"

    name: Mapped[str | None] = mapped_column(Text)
    zone_type: Mapped[str | None] = mapped_column(Text)
    polygon_json: Mapped[str | None] = mapped_column(Text)
