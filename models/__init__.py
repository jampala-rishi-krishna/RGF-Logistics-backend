"""Import every model module so Base.metadata is complete for Alembic autogenerate."""

from database import Base  # noqa: F401
from models import admin, alert, comms, dispatch, dispatch_pipeline, inventory, optimization, order, route, sales_order_history, sales_order_lines, user, vehicle, warehouse  # noqa: F401

__all__ = ["Base", "user", "vehicle", "route", "order", "alert", "comms", "warehouse", "admin", "optimization", "inventory", "dispatch", "dispatch_pipeline", "sales_order_history", "sales_order_lines"]
