"""Fleet Health tables (migration j1b2c3d4e5f6). Tiny daily/event rows only: no raw trips or events.

Staff live in n8n, not Neon, so staff ids are plain integers (no FK).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase, utcnow


class VehicleDailyStat(TimestampedBase):
    __tablename__ = "vehicle_daily_stats"
    __table_args__ = (UniqueConstraint("vehicle_id", "stat_date", name="uq_vehicle_daily_stats_vehicle_date"),)

    vehicle_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    odometer_start_km: Mapped[float | None] = mapped_column(Numeric(10, 2))
    odometer_end_km: Mapped[float | None] = mapped_column(Numeric(10, 2))
    km_driven: Mapped[float | None] = mapped_column(Numeric(10, 2))
    trip_count: Mapped[int | None] = mapped_column(Integer)
    engine_seconds: Mapped[int | None] = mapped_column(Integer)
    idle_seconds_total: Mapped[int | None] = mapped_column(Integer)
    idle_seconds_at_stop: Mapped[int | None] = mapped_column(Integer)
    idle_seconds_elsewhere: Mapped[int | None] = mapped_column(Integer)
    speeding_events: Mapped[int | None] = mapped_column(Integer)
    speeding_seconds: Mapped[int | None] = mapped_column(Integer)
    max_speed_kmh: Mapped[int | None] = mapped_column(Integer)
    harsh_braking: Mapped[int | None] = mapped_column(Integer)
    harsh_acceleration: Mapped[int | None] = mapped_column(Integer)
    harsh_cornering: Mapped[int | None] = mapped_column(Integer)
    vext_parked_min: Mapped[float | None] = mapped_column(Numeric(5, 2))
    vext_running_avg: Mapped[float | None] = mapped_column(Numeric(5, 2))
    electrical_system: Mapped[int | None] = mapped_column(SmallInteger)
    fuel_pct_start: Mapped[float | None] = mapped_column(Numeric(5, 2))
    fuel_pct_end: Mapped[float | None] = mapped_column(Numeric(5, 2))
    refuel_events: Mapped[int | None] = mapped_column(Integer)
    refuel_litres_est: Mapped[float | None] = mapped_column(Numeric(7, 2))
    parked_drop_litres_est: Mapped[float | None] = mapped_column(Numeric(7, 2))
    primary_staff_id: Mapped[int | None] = mapped_column(Integer)
    assigned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    data_quality: Mapped[dict | None] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ServiceInterval(TimestampedBase):
    __tablename__ = "service_intervals"

    vehicle_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("vehicles.id", ondelete="CASCADE"))
    service_type: Mapped[str] = mapped_column(String(24), nullable=False)
    interval_km: Mapped[int | None] = mapped_column(Integer)
    interval_engine_hours: Mapped[int | None] = mapped_column(Integer)
    interval_days: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))


class MaintenanceRecord(TimestampedBase):
    __tablename__ = "maintenance_records"

    vehicle_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    service_type: Mapped[str | None] = mapped_column(String(24))
    performed_on: Mapped[date] = mapped_column(Date, nullable=False)
    odometer_km: Mapped[float | None] = mapped_column(Numeric(10, 2))
    engine_hours: Mapped[float | None] = mapped_column(Numeric(10, 2))
    downtime_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    downtime_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    cost_php: Mapped[float | None] = mapped_column(Numeric(12, 2))
    vendor: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    receipt_ref: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))


class VehicleFlag(TimestampedBase):
    __tablename__ = "vehicle_flags"
    # Dedup only among OPEN flags (same partial unique index as migration j1b2c3d4e5f6).
    __table_args__ = (Index("ux_vehicle_flags_open_dedup", "dedup_key", unique=True, postgresql_where=text("resolved_at IS NULL"), sqlite_where=text("resolved_at IS NULL")),)

    vehicle_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[str | None] = mapped_column(Text)
    photo_ref: Mapped[str | None] = mapped_column(Text)
    reported_by: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedup_key: Mapped[str] = mapped_column(String(40), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    resolution_note: Mapped[str | None] = mapped_column(Text)


class PretripChecklist(TimestampedBase):
    __tablename__ = "pretrip_checklists"

    vehicle_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False)
    staff_id: Mapped[int | None] = mapped_column(Integer)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entered_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    items: Mapped[dict] = mapped_column(JSON, nullable=False)
    reefer_temp_c: Mapped[float | None] = mapped_column(Numeric(5, 1))
    notes: Mapped[str | None] = mapped_column(Text)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)


class FuelLog(TimestampedBase):
    __tablename__ = "fuel_logs"

    vehicle_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False)
    staff_id: Mapped[int | None] = mapped_column(Integer)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    litres: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)
    amount_php: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    odometer_km: Mapped[float | None] = mapped_column(Numeric(10, 2))
    full_tank: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    station: Mapped[str | None] = mapped_column(Text)
    receipt_ref: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
