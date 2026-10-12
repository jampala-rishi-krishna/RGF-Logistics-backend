"""Driver vs helper, derived from the staff directory and from stored assignment rows.

Stored shape (unchanged, no migration): sales_orders.driver_id = the driver; sales_orders.helper_ids = every selected
staff id INCLUDING the driver (kept that way for backward compatibility). Helpers are therefore always
`set(helper_ids) - {driver_id}`, by value - never "everything after the first element".
"""
from __future__ import annotations


def helper_ids_of(driver_id: int | None, helper_ids) -> list[int]:
    """Helper staff ids of an assignment, sorted so the result never depends on the order of `helper_ids`."""
    values = {int(h) for h in (helper_ids or []) if h is not None}
    if driver_id is not None:
        values.discard(int(driver_id))
    return sorted(values)


def staff_kind(staff: dict) -> str:
    """'helper' for a staff-directory row with role HELPER or title DELIVERY HELPER, else 'driver'."""
    role = str(staff.get("role") or "").strip().upper()
    title = str(staff.get("title") or "").strip().upper()
    return "helper" if role == "HELPER" or title == "DELIVERY HELPER" else "driver"
