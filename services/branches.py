"""Zoho branches (a.k.a. locations) - config only, never stored in Neon.

A sales order's branch comes from Zoho's own `branch_id` / `branch_name` on the record (list rows and
detail both carry them). It is NEVER inferred from the order-number prefix: the Sari-suki series is
"SS SO26-..." and "WM-SO26-..." (Wet Market) is a numbering series inside the RGF branch.

IDs were read from Zoho's locations API on 2026-10-08. Unknown branch ids that show up in data are
still handled (see `describe`): they are shown with their Zoho name and an initials badge.
"""
from __future__ import annotations

import os
import re

# stock: how Mets/Glacier columns map to this branch's warehouses (used only when
# BRANCH_STOCK_MAPPING is enabled; see services/warehouse_stock.py).
#   mets / glacier  -> substrings that must ALL appear in the (casefolded) warehouse name
#   other           -> exact (casefolded) warehouse name shown as the branch's own warehouse
BRANCHES: list[dict] = [
    {"id": "4489499000001322444", "name": "Rare Global Food Trading Corp.", "code": "RGF", "label": "RGF"},
    {"id": "4489499000017295785", "name": "Meat and Seafood Specialist Inc.", "code": "MSSI", "label": "Meat and Seafood (MSSI)"},
    {"id": "4489499000044793937", "name": "SariSuki Store Inc.", "code": "SSI", "label": "SariSuki (SSI)"},
    {"id": "4489499000017304175", "name": "Rare Cuts", "code": "RC", "label": "Rare Cuts"},
    {"id": "4489499000267238651", "name": "Rare Food Shop", "code": "RFS", "label": "Rare Food Shop"},
]
_BY_ID = {b["id"]: b for b in BRANCHES}

# Per-branch warehouse -> column mapping. Matching is on the casefolded warehouse name.
STOCK_RULES: dict[str, dict] = {
    "4489499000001322444": {"mets": ("mets cold storage",), "mets_exclude": ("near-expiry", "for supermarket", "mssi"), "glacier": ("glacier south rgf",)},
    "4489499000017295785": {"mets": ("mets cold storage", "mssi"), "glacier": ("glacier south mssi",)},
    "4489499000044793937": {"other": "sarisuki store inc. warehouse"},
    "4489499000017304175": {"other": "production area rc"},
    "4489499000267238651": {"other": "glacier rfs"},
}


def stock_mapping_enabled() -> bool:
    return os.environ.get("BRANCH_STOCK_MAPPING", "").strip().lower() in {"1", "true", "yes", "on"}


def _initials(name: str) -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name or "") if w]
    return ("".join(w[0] for w in words)[:4] or "?").upper()


def describe(branch_id, branch_name=None) -> dict | None:
    """{id, name, code, label} for a branch id; unknown ids fall back to the Zoho name."""
    branch_id = str(branch_id or "").strip()
    if not branch_id:
        return None
    known = _BY_ID.get(branch_id)
    if known:
        return dict(known)
    name = str(branch_name or "").strip() or branch_id
    return {"id": branch_id, "name": name, "code": _initials(name), "label": name}


def branch_of(row_or_record) -> dict | None:
    """The branch of a Zoho record dict or a cached row (reads its raw_json). None when absent -
    e.g. past-dated rows read from Neon, which keep no raw_json."""
    record = row_or_record if isinstance(row_or_record, dict) else (getattr(row_or_record, "raw_json", None) or {})
    if not isinstance(record, dict):
        return None
    return describe(record.get("branch_id") or record.get("location_id"), record.get("branch_name") or record.get("location_name"))


def branch_id_of(row_or_record) -> str | None:
    found = branch_of(row_or_record)
    return found["id"] if found else None


def all_options(seen: list[dict] | None = None) -> list[dict]:
    """Config branches (always all of them) plus any unknown branch seen in data."""
    options = [dict(b) for b in BRANCHES]
    known = {b["id"] for b in options}
    for extra in seen or []:
        if extra and extra["id"] not in known:
            options.append(dict(extra))
            known.add(extra["id"])
    return options


def parse_ids(value) -> set[str]:
    # Non-str (e.g. an unresolved FastAPI Query default when a route function is called directly) = no filter.
    if not isinstance(value, str):
        return set()
    return {part.strip() for part in value.split(",") if part.strip()}


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def squash_number(value) -> str:
    """Casefolded, alphanumerics only: 'SS SO26-16612', 'ss-so26-16612' and 'SSSO2616612' all match."""
    return _NON_ALNUM.sub("", str(value or "").casefold())
