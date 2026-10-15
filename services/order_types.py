"""The four order types Load Planning handles, and the one rule that tells them apart.

  SO  sales order (Zoho Inventory sales orders list)
  WM  wet market: a sales order whose number starts with "WM-SO" - the SAME Zoho list as SO, split by number prefix (no extra Zoho call)
  PO  purchase order (id "PO:<zoho id>", services/purchase_orders.py)
  TO  transfer order (id "TO:<zoho id>", services/transfer_orders.py)

SO and WM behave identically (acknowledgement, branch stock, invoice at check-out). PO and TO are "reference" orders: no Zoho SO status or
acknowledgement, no branch stock, no invoice at check-out (their own number is the reference), and the crew's check-out completes them.
"""
from __future__ import annotations

TYPES = ("SO", "WM", "PO", "TO")
SALES_TYPES = frozenset({"SO", "WM"})
REFERENCE_TYPES = frozenset({"PO", "TO"})
DEFAULT_LIST_TYPES = frozenset(SALES_TYPES)  # an absent order_types filter = every sales order (SO and WM), as before WM was split out
WM_PREFIX = "WM-SO"
_ID_PREFIXES = {"TO:": "TO", "PO:": "PO"}


def _s(value) -> str:
    return "" if value is None else str(value).strip()


def is_wm_number(number) -> bool:
    return _s(number).upper().startswith(WM_PREFIX)


def type_of_id(order_id) -> str | None:
    """'TO' / 'PO' for an id carrying that prefix, else None (a plain Zoho sales-order id can be SO or WM)."""
    text = _s(order_id)
    for prefix, kind in _ID_PREFIXES.items():
        if text.startswith(prefix):
            return kind
    return None


def order_type_of(row, number=None) -> str:
    """'SO' | 'WM' | 'PO' | 'TO' for a live row, a Neon sales_orders row, a summary dict or a plain id (+ optional number).
    An explicit WM/PO/TO wins; then the id prefix; then a WM-SO number (so WM rows saved as 'SO' before the split still read as WM)."""
    if isinstance(row, str):
        return type_of_id(row) or ("WM" if is_wm_number(number) else "SO")
    get = row.get if isinstance(row, dict) else (lambda key, default=None: getattr(row, key, default))
    explicit = _s(get("order_type")).upper()
    if explicit in ("TO", "PO", "WM"):
        return explicit
    by_id = type_of_id(get("id"))
    if by_id:
        return by_id
    return "WM" if is_wm_number(number if number is not None else get("salesorder_number")) else "SO"


def is_reference_type(kind) -> bool:
    return _s(kind).upper() in REFERENCE_TYPES


def parse_types(value, default=DEFAULT_LIST_TYPES) -> set[str]:
    """'SO,WM' -> {'SO', 'WM'}; unknown codes are ignored; empty / absent (or not a str, e.g. an unresolved FastAPI Query) -> default."""
    wanted = {part.strip().upper() for part in (value if isinstance(value, str) else "").split(",") if part.strip()} & set(TYPES)
    return wanted or set(default)


def empty_counts() -> dict[str, int]:
    return {kind: 0 for kind in TYPES}
