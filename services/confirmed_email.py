"""Confirmed-orders warehouse email: preview + send package (PDF + Excel) for ONE warehouse.

Stateless: nothing is persisted (no tables). Orders come from the live caches; the files are always
regenerated server-side from (order_ids, warehouse) - client-supplied files are never trusted.
Only the chosen warehouse's lines are exported; the other warehouse's data is excluded entirely.
"""
from __future__ import annotations

import base64
import contextvars
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from services import live_sales_order_cache
from services.inventory_exports import CONFIRMED_HEADERS, CONFIRMED_TYPE_COLUMN, make_excel, make_pdf, order_line_warehouse, order_type_of, sort_confirmed_rows
from services.warehouses import WAREHOUSES, warehouse_key

PHT = ZoneInfo("Asia/Manila")
MAX_RECIPIENTS = 25
MAX_ORDERS = 500
VALID_WAREHOUSES = ("mets", "glacier")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


class ConfirmedEmailError(Exception):
    """A request problem; `status_code` is what the API answers with."""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def normalize_warehouse(value) -> str:
    key = str(value or "").strip().lower()
    if key not in VALID_WAREHOUSES:
        raise ConfirmedEmailError("Choose a warehouse: 'mets' or 'glacier'.")
    return key


def normalize_recipients(value, label: str, required: bool = False) -> list[str]:
    """list or comma/semicolon separated string -> de-duplicated (case-insensitive) valid addresses."""
    items = [value] if isinstance(value, str) else list(value or [])
    cleaned = [part.strip() for item in items for part in re.split(r"[;,]", str(item or "")) if part.strip()]
    bad = [item for item in cleaned if len(item) > 254 or ".." in item or not _EMAIL_RE.match(item)]
    if bad:
        raise ConfirmedEmailError(f"Invalid {label} email address: {', '.join(bad)}")
    unique: dict[str, str] = {}
    for item in cleaned:
        unique.setdefault(item.lower(), item)
    result = list(unique.values())
    if required and not result:
        raise ConfirmedEmailError(f"At least one '{label}' email address is required.")
    if len(result) > MAX_RECIPIENTS:
        raise ConfirmedEmailError(f"Too many '{label}' addresses (maximum {MAX_RECIPIENTS}).")
    return result


class _FilteredOrder:
    """Read-only view of an order whose raw_json holds only the chosen warehouse's lines."""

    def __init__(self, order, raw_json: dict):
        self._order = order
        self.raw_json = raw_json

    def __getattr__(self, name):
        return getattr(self._order, name)


def _line_key(order, item: dict) -> str | None:
    key = warehouse_key(order_line_warehouse(order, item))
    if key is None and order_type_of(order) == "TO":
        raw = getattr(order, "raw_json", None) or {}
        source = str(raw.get("source_warehouse_key") or "").lower()
        key = source if source in WAREHOUSES else None
    return key


def filter_orders_for_warehouse(orders, warehouse: str) -> list:
    """Keep only lines picked from `warehouse`; a mixed SO contributes just its lines for this warehouse;
    orders left with zero lines are dropped. Lines with no recognisable warehouse are excluded."""
    result = []
    for order in orders:
        raw = getattr(order, "raw_json", None) or {}
        lines = [item for item in (raw.get("line_items") or []) if isinstance(item, dict) and _line_key(order, item) == warehouse]
        if lines:
            result.append(_FilteredOrder(order, {**raw, "line_items": lines}))
    return result


def load_orders(db, order_ids) -> list:
    ids = list(dict.fromkeys(str(value).strip() for value in (order_ids or []) if str(value or "").strip()))
    if not ids:
        raise ConfirmedEmailError("Select at least one order.")
    if len(ids) > MAX_ORDERS:
        raise ConfirmedEmailError(f"Too many orders selected (maximum {MAX_ORDERS}).")

    def fetch(order_id: str):
        try:
            return live_sales_order_cache.find_cached(order_id) or live_sales_order_cache.ensure_zoho_data(order_id)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(contextvars.copy_context().run, fetch, order_id) for order_id in ids]
        orders = [future.result() for future in futures]
    orders = [order for order in orders if order is not None]
    from routers import load_planning  # lazy: the router module imports a lot
    return load_planning._hydrate_export_rows(db, orders)


def _json_safe(value):
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)


def build_package(db, warehouse, order_ids, date_from: str | None = None, date_to: str | None = None) -> dict:
    warehouse = normalize_warehouse(warehouse)
    orders = filter_orders_for_warehouse(load_orders(db, order_ids), warehouse)
    from routers import load_planning
    rows = sort_confirmed_rows(load_planning._confirmed_export_rows(orders)) if orders else []
    if not rows:
        raise ConfirmedEmailError(f"None of the selected orders have lines picked from {WAREHOUSES[warehouse]['name']}.")
    name = WAREHOUSES[warehouse]["name"]
    today = datetime.now(PHT).date().isoformat()
    start = date_from or today
    end = date_to or start
    caption = f"{name} - {start} to {end} - Confirmed orders - Generated {datetime.now(PHT).strftime('%Y-%m-%d %H:%M')} PHT"
    pdf = make_pdf(rows, caption, "confirmed").getvalue()
    xlsx = make_excel(rows, caption, "confirmed", warehouse_fill=False).getvalue()
    counts: dict[str, int] = {}
    for order in orders:
        counts[order_type_of(order)] = counts.get(order_type_of(order), 0) + 1
    stem = f"RGF_Confirmed_{warehouse.capitalize()}_{start}" + (f"_to_{end}" if end != start else "")
    return {
        "warehouse": warehouse,
        "order_count": len(orders),
        "line_count": len(rows),
        "counts_by_type": counts,
        "headers": list(CONFIRMED_HEADERS),
        "rows": [[_json_safe(cell) for cell in row] for row in rows],
        "pdf": pdf,
        "xlsx": xlsx,
        "pdf_filename": f"{stem}.pdf",
        "xlsx_filename": f"{stem}.xlsx",
    }


def preview_payload(package: dict) -> dict:
    out = {key: value for key, value in package.items() if key not in ("pdf", "xlsx")}
    out["pdf_base64"] = base64.b64encode(package["pdf"]).decode("ascii")
    out["xlsx_base64"] = base64.b64encode(package["xlsx"]).decode("ascii")
    return out
