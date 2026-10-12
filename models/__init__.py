"""Import every model module so Base.metadata is complete for Alembic autogenerate."""

from database import Base  # noqa: F401
from models import admin, alert, app_setting, comms, dispatch, dispatch_pipeline, fleet_health, inventory, optimization, order, route, sales_order_history, sales_order_lines, trip, user, vehicle, warehouse  # noqa: F401

__all__ = ["Base", "user", "vehicle", "route", "order", "alert", "comms", "warehouse", "admin", "optimization", "inventory", "dispatch", "dispatch_pipeline", "fleet_health", "sales_order_history", "sales_order_lines", "trip", "app_setting"]
