from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


class VehicleOperatingProfile(TimestampedBase):
    __tablename__ = "vehicle_operating_profiles"

    vehicle_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    capacity_kg: Mapped[float | None] = mapped_column(Numeric(10, 2))
    capacity_m3: Mapped[float | None] = mapped_column(Numeric(10, 2))
    temperature_capability: Mapped[str | None] = mapped_column(Text)
    shift_start: Mapped[str | None] = mapped_column(Text)
    shift_end: Mapped[str | None] = mapped_column(Text)
    cost_per_km: Mapped[float | None] = mapped_column(Numeric(10, 2))
    cost_per_hour: Mapped[float | None] = mapped_column(Numeric(10, 2))
    depot_lat: Mapped[float | None] = mapped_column(Numeric(9, 6))
    depot_lng: Mapped[float | None] = mapped_column(Numeric(9, 6))


class OptimizationRun(TimestampedBase):
    __tablename__ = "optimization_runs"

    status: Mapped[str | None] = mapped_column(Text)
    objective: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope: Mapped[str | None] = mapped_column(Text)
    data_snapshot: Mapped[str | None] = mapped_column(Text)


class OptimizationRunRoute(TimestampedBase):
    __tablename__ = "optimization_run_routes"

    run_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    vehicle_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    distance: Mapped[float | None] = mapped_column(Numeric(10, 2))
    duration: Mapped[float | None] = mapped_column(Numeric(10, 2))
    risk: Mapped[int | None] = mapped_column(Integer)


class OptimizationRunStop(TimestampedBase):
    __tablename__ = "optimization_run_stops"

    run_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    vehicle_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    order_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    stop_sequence: Mapped[int | None] = mapped_column(Integer)
    location_name: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Numeric(9, 6))
    lng: Mapped[float | None] = mapped_column(Numeric(9, 6))
    eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slack_min: Mapped[float | None] = mapped_column(Numeric(10, 2))
