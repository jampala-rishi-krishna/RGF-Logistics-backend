from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.vehicle import Vehicle
from services import live_gps_store
from services import live_sales_order_cache
from services.google_maps import geocode_address
from services.item_weight import calculate_order_weight_kg
from services.sales_order_location import address_lines
from services.time_utils import minutes_since_midnight
from services.warehouses import get_warehouse

logger = logging.getLogger("optimization_data")

GPS_STALENESS_THRESHOLD_MIN = 20


class FleetDataError(Exception):
    """Mirrors the Node module's thrown Errors - message is user-facing (missing prerequisite,
    stale GPS, no coordinates, etc.)."""

    def __init__(self, message: str, issues: list[dict] | None = None):
        super().__init__(message)
        self.issues = issues or []


WEIGHT_TO_KG = {"kg": 1.0, "kilogram": 1.0, "kilograms": 1.0, "g": 0.001, "gram": 0.001,
                "grams": 0.001, "lb": 0.45359237, "lbs": 0.45359237, "pound": 0.45359237,
                "pounds": 0.45359237, "t": 1000.0, "tonne": 1000.0, "tonnes": 1000.0,
                "ton": 1000.0, "tons": 1000.0}


def normalize_weight_kg(value: float | None, unit: str | None) -> float | None:
    if value is None or value <= 0 or not unit:
        return None
    multiplier = WEIGHT_TO_KG.get(unit.strip().lower())
    return round(float(value) * multiplier, 3) if multiplier is not None else None


@dataclass
class VehiclePlan:
    id: int
    capacity_kg: float
    cost_per_km: float | None
    cost_per_hour: float | None
    start_lat: float
    start_lng: float
    end_lat: float
    end_lng: float
    has_end_location: bool
    shift_start: int
    shift_end: int
    temperature_capabilities: list[str]


@dataclass
class ShipmentPlan:
    id: int
    order_id: int
    location_name: str
    lat: float
    lng: float
    demand_kg: float
    service_time_min: float
    time_window_start: int
    time_window_end: int
    temperature_requirement: str
    priority: int


@dataclass
class FleetData:
    vehicles: list[VehiclePlan]
    shipments: list[ShipmentPlan]
    warnings: list[str] = field(default_factory=list)
    data_snapshot: dict = field(default_factory=dict)
    return_to_warehouse: bool = False
    return_warehouse_id: str | None = None
    return_warehouse: dict | None = None


async def fetch_fleet_data(
    db: Session,
    *,
    mode: str,
    vehicle_ids: list | None,
    order_ids: list | None = None,
    return_to_warehouse: bool = False,
    return_warehouse_id: str | None = None,
) -> FleetData:
    vehicles_stmt = select(Vehicle)
    if vehicle_ids:
        vehicles_stmt = vehicles_stmt.where(Vehicle.id.in_([int(v) for v in vehicle_ids]))
    vehicles = db.execute(vehicles_stmt).scalars().all()
    if not vehicles:
        raise FleetDataError("No vehicles found in the fleet.")

    warnings: list[str] = []
    return_warehouse = get_warehouse(return_warehouse_id) if return_to_warehouse else None
    vehicle_list: list[VehiclePlan] = []
    vehicle_fingerprints: list[str] = []
    now = datetime.now(timezone.utc)

    for v in vehicles:
        if v.capacity_kg is None:
            # The dispatch capacity field remains the fallback for existing rosters.
            v_capacity = v.rated_capacity_kg
        else:
            v_capacity = v.capacity_kg
        if v_capacity is None:
            continue

        live = live_gps_store.get(str(v.plate_no or "").strip().upper())

        if mode == "reoptimize":
            if live is None:
                raise FleetDataError(f"Missing prerequisite: Vehicle {v.plate_no} is missing current Cartrack GPS coordinates.")
            gps_age_min = (now - live["last_updated"]).total_seconds() / 60.0
            if gps_age_min > GPS_STALENESS_THRESHOLD_MIN or gps_age_min < -1:
                raise FleetDataError(
                    f"Vehicle {v.plate_no}'s GPS position is stale (last updated {round(gps_age_min)} min ago, "
                    f"threshold is {GPS_STALENESS_THRESHOLD_MIN} min). Cannot use it as a live reoptimization start point."
                )
            start_lat, start_lng = live["lat"], live["lng"]
        else:
            if v.depot_lat is not None and v.depot_lng is not None:
                start_lat, start_lng = float(v.depot_lat), float(v.depot_lng)
            elif live is not None:
                warnings.append(f"Vehicle {v.plate_no}: no depot location configured - using current GPS position as the planning start point.")
                start_lat, start_lng = live["lat"], live["lng"]
            else:
                raise FleetDataError(f"Missing prerequisite: Vehicle {v.plate_no} has no depot_lat/depot_lng in vehicle_operating_profiles and no current GPS position.")

        vehicle_list.append(
            VehiclePlan(
                id=v.id,
                capacity_kg=float(v_capacity),
                cost_per_km=float(v.cost_per_km) if v.cost_per_km is not None else None,
                cost_per_hour=float(v.cost_per_hour) if v.cost_per_hour is not None else None,
                start_lat=start_lat,
                start_lng=start_lng,
                end_lat=float(return_warehouse["lat"]) if return_warehouse else start_lat,
                end_lng=float(return_warehouse["lng"]) if return_warehouse else start_lng,
                has_end_location=bool(return_warehouse),
                shift_start=minutes_since_midnight(v.shift_start) or 0,
                shift_end=minutes_since_midnight(v.shift_end) or 1440,
                temperature_capabilities=(v.temperature_capability.split(",") if v.temperature_capability else ["ambient"]),
            )
        )
        # updated_at is bumped by SQLAlchemy's onupdate=utcnow on ANY column change (capacity,
        # shift, cost rates, depot, GPS, ...) - using it (rather than hand-picking fields) is
        # what makes the staleness check actually catch every input the plan depended on.
        vehicle_fingerprints.append(f"{v.id}:{v.updated_at}:{v.capacity_kg}:{v.depot_lat}:{v.depot_lng}")

    if not vehicle_list:
        raise FleetDataError("No eligible vehicles with operating profiles and valid start coordinates found.")

    orders = [o for o in live_sales_order_cache.get_current_orders() if not o.route_id]
    if order_ids is not None:
        wanted = {str(order_id) for order_id in order_ids}
        orders = [o for o in orders if str(o.id) in wanted or str(o.salesorder_number) in wanted]
    if not orders:
        raise FleetDataError("No current Zoho sales orders found to optimize.")

    shipments: list[ShipmentPlan] = []
    missing_coord_orders: list[str] = []
    data_quality_issues: list[dict] = []
    order_fingerprints: list[str] = []

    for order in orders:
        # updated_at catches edits to weight/service-time/eta-window/temperature/etc, not just
        # the status/vehicle_id transitions the previous fingerprint tracked - a preview must go
        # stale if any input the solver actually used changes, not only if it's reassigned.
        raw = order.raw_json or {}
        address = raw.get("shipping_address") or {}
        if isinstance(address, list): address = address[0] if address else {}
        name = order.customer_name or f"Order {order.id}"
        lat = raw.get("latitude") or raw.get("lat") or (address.get("latitude") if isinstance(address, dict) else None)
        lng = raw.get("longitude") or raw.get("lng") or (address.get("longitude") if isinstance(address, dict) else None)
        # Full street text (incl. Zoho's street2) geocodes far better than the first line alone;
        # business/attention names are left out because they only confuse the geocoder.
        address_text = ", ".join(address_lines(address, include_names=False))
        if lat is None or lng is None:
            geocoded = await geocode_address(address_text) if address_text else None
            if geocoded:
                lat, lng = geocoded["lat"], geocoded["lng"]
            else:
                missing_coord_orders.append(str(order.id))
                continue

        order_fingerprints.append(f"{order.id}:{order.assignment_status}:{order.vehicle_id or ''}:{order.synced_at}")
        demand_kg = calculate_order_weight_kg(order)
        if demand_kg is None:
            missing_coord_orders.append(order.id)
            data_quality_issues.append({
                "code": "MISSING_SHIPMENT_WEIGHT",
                "order_id": order.id,
                "shipment_id": None,
                "customer": name,
                "current_value": None,
                "required_field": "Zoho line-item quantity and weight/package metadata",
            })
            continue

        shipments.append(
            ShipmentPlan(
                id=order.id,
                order_id=order.id,
                location_name=name,
                lat=float(lat),
                lng=float(lng),
                demand_kg=demand_kg,
                service_time_min=30.0,
                time_window_start=0,
                time_window_end=1440,
                temperature_requirement=("chilled" if "chill" in str(raw).lower() else "ambient"),
                priority=1,
            )
        )

    if data_quality_issues:
        missing_weight = sum(1 for issue in data_quality_issues if issue["code"] == "MISSING_SHIPMENT_WEIGHT")
        missing_service = sum(1 for issue in data_quality_issues if issue["code"] == "MISSING_SERVICE_TIME")
        summary = []
        if missing_weight:
            summary.append(f"{missing_weight} orders are missing shipment weight.")
        if missing_service:
            summary.append(f"{missing_service} orders are missing service time.")
        raise FleetDataError(" ".join(summary), data_quality_issues)

    if missing_coord_orders:
        raise FleetDataError(
            f"{len(missing_coord_orders)} deliveries require valid coordinates. "
            f"Orders: {', '.join(str(i) for i in missing_coord_orders)}. Geocoding failed or the customer has no address on file."
        )

    data_snapshot = {"vehicles": sorted(vehicle_fingerprints), "orders": sorted(order_fingerprints), "return_to_warehouse": bool(return_warehouse), "return_warehouse_id": return_warehouse_id if return_warehouse else None}
    return FleetData(vehicles=vehicle_list, shipments=shipments, warnings=warnings, data_snapshot=data_snapshot, return_to_warehouse=bool(return_warehouse), return_warehouse_id=return_warehouse_id if return_warehouse else None, return_warehouse=return_warehouse)
