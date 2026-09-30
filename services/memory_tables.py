"""Step 5/6 (2026-09-24): in-memory replacements for tables dropped from Neon that still
have a live feature reading/writing them. Each store is a simple dict + auto-increment id,
process-local (lost on restart) - acceptable for these: none of them are business-critical
persisted state (that's users/vehicles/sales_orders/sales_order_lines/
client_delivery_constraints/load_manifests/manifest_items/warehouse_loading_checklists,
which stay in Neon)."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

_lock = threading.Lock()


class AutoIncrementStore:
    def __init__(self):
        self._rows: dict[int, dict] = {}
        self._next_id = 1

    def create(self, **fields) -> dict:
        with _lock:
            row = {"id": self._next_id, "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc), **fields}
            self._rows[self._next_id] = row
            self._next_id += 1
            return dict(row)

    def get(self, row_id: int) -> dict | None:
        with _lock:
            row = self._rows.get(row_id)
            return dict(row) if row else None

    def update(self, row_id: int, **fields) -> dict | None:
        with _lock:
            row = self._rows.get(row_id)
            if row is None:
                return None
            row.update(fields)
            row["updated_at"] = datetime.now(timezone.utc)
            return dict(row)

    def delete(self, row_id: int) -> bool:
        with _lock:
            return self._rows.pop(row_id, None) is not None

    def list(self, **filters) -> list[dict]:
        with _lock:
            rows = [dict(r) for r in self._rows.values()]
        for key, value in filters.items():
            if value is not None:
                rows = [r for r in rows if r.get(key) == value]
        return rows


# --- Route optimization runs (optimization_runs/optimization_run_routes/optimization_run_stops) ---
optimization_runs = AutoIncrementStore()
optimization_run_routes = AutoIncrementStore()
optimization_run_stops = AutoIncrementStore()

# --- Route Optimization applied routes (routes/route_stops) ---
routes = AutoIncrementStore()
route_stops = AutoIncrementStore()

# --- Legacy customer-portal order tracking (orders/customers/order_events/delivery_proofs) ---
orders = AutoIncrementStore()
customers = AutoIncrementStore()
order_events = AutoIncrementStore()
delivery_proofs = AutoIncrementStore()

# --- Dispatch Communications Gateway config (message_templates/customer_contacts) ---
message_templates = AutoIncrementStore()
customer_contacts = AutoIncrementStore()

# --- Legacy simulated-comms notification templates (the one surviving comms.py endpoint) ---
notification_templates = AutoIncrementStore()

# --- Communications Gateway timeline (message_log) ---
message_log = AutoIncrementStore()

# --- AI chat assistant history (conversations/messages) ---
conversations = AutoIncrementStore()
messages = AutoIncrementStore()

# --- Admin: integration health checks (integration_status), keyed by provider not id ---
_integration_lock = threading.Lock()
_integration_status: dict[str, dict] = {}


def upsert_integration_status(provider: str, state: str) -> dict:
    with _integration_lock:
        row = {"provider": provider, "state": state, "last_checked": datetime.now(timezone.utc)}
        _integration_status[provider] = row
        return dict(row)


def list_integration_status() -> list[dict]:
    with _integration_lock:
        return [dict(r) for r in _integration_status.values()]


# --- Admin: roles (static reference data, never written) ---
ROLES = [
    {"id": 1, "name": "admin", "description": "Full access to every tab"},
    {"id": 2, "name": "dispatcher", "description": "Assignment, dispatch, and communications"},
    {"id": 3, "name": "warehouse", "description": "Warehouse loading and manifest operations"},
]

# --- Fleet: drivers/geofences reference data (never written by any live code path) ---
DRIVERS: list[dict] = []
GEOFENCES: list[dict] = []
