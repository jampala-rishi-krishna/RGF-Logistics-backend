from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


class Route(TimestampedBase):
    __tablename__ = "routes"

    name: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str | None] = mapped_column(Text)
    distance_km: Mapped[float | None] = mapped_column(Float)
    duration_min: Mapped[float | None] = mapped_column(Float)
    cost: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(Text)
    # Optional in Catalyst (schema-probed at runtime there); always present here.
    polyline_geojson: Mapped[str | None] = mapped_column(Text)


class RouteStop(TimestampedBase):
    __tablename__ = "route_stops"

    route_id: Mapped[str | None] = mapped_column(String(200), index=True)
    sequence: Mapped[int | None] = mapped_column(Integer)
    location_name: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    arrival_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Fixed defect: Catalyst had this misspelled "rrival_window_end".
    arrival_window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoadManifest(TimestampedBase):
    __tablename__ = "load_manifests"

    route_id: Mapped[str | None] = mapped_column(String(200), index=True)
    vehicle_id: Mapped[str | None] = mapped_column(String(200), index=True)
    status: Mapped[str | None] = mapped_column(Text)
    cargo_type: Mapped[str | None] = mapped_column(Text)
    manifest_number: Mapped[str | None] = mapped_column(String(80), unique=True)
    driver_id: Mapped[int | None] = mapped_column(Integer, index=True)
    origin: Mapped[str | None] = mapped_column(Text)
    destination: Mapped[str | None] = mapped_column(Text)
    total_weight_kg: Mapped[float | None] = mapped_column(Float)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[int | None] = mapped_column(Integer)


class ManifestItem(TimestampedBase):
    __tablename__ = "manifest_items"

    manifest_id: Mapped[str | None] = mapped_column(String(200), index=True)
    cargo_category: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[int | None] = mapped_column(Integer)
    temp_requirement_c: Mapped[float | None] = mapped_column(Float)
    salesorder_id: Mapped[str | None] = mapped_column(String(200), index=True)
    item_description: Mapped[str | None] = mapped_column(Text)
    sku: Mapped[str | None] = mapped_column(String(200))
    unit: Mapped[str | None] = mapped_column(String(80))
    weight_kg: Mapped[float | None] = mapped_column(Float)
    cold_chain_category: Mapped[str | None] = mapped_column(String(32))
