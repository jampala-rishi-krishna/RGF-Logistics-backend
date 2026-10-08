"""Zoho branches (a.k.a. locations) - config only, never stored in Neon.

A sales order's branch comes from Zoho's own `branch_id` / `branch_name` on the record (list rows and
detail both carry them). It is NEVER inferred from the order-number prefix: the Sari-suki series is
"SS SO26-..." and "WM-SO26-..." (Wet Market) is a numbering series inside the RGF branch.

IDs were read from Zoho's locations API on 2026-10-08.

ALLOWLIST: RareChain handles only RGF and Meat and Seafood Specialist Inc. (MSSI). Orders of every
other branch (SariSuki, Rare Cuts, Rare Food Shop, and any branch never seen before) are never
loaded, counted, cached, hydrated, listed, exported or acted on. Override with env ALLOWED_BRANCHES
(comma-separated branch ids).
"""
from __future__ import annotations

import logging
import os
import re
import threading

from fastapi import HTTPException

logger = logging.getLogger("branches")

# Directory of known Zoho branches (labels/badges). Only ALLOWED_BRANCH_IDS are ever loaded or offered.
BRANCHES: list[dict] = [
    {"id": "4489499000001322444", "name": "Rare Global Food Trading Corp.", "code": "RGF", "label": "RGF"},
    {"id": "4489499000017295785", "name": "Meat and Seafood Specialist Inc.", "code": "MSSI", "label": "Meat and Seafood (MSSI)"},
    {"id": "4489499000044793937", "name": "SariSuki Store Inc.", "code": "SSI", "label": "SariSuki (SSI)"},
    {"id": "4489499000017304175", "name": "Rare Cuts", "code": "RC", "label": "Rare Cuts"},
    {"id": "4489499000267238651", "name": "Rare Food Shop", "code": "RFS", "label": "Rare Food Shop"},
]
_BY_ID = {b["id"]: b for b in BRANCHES}

# Per-branch warehouse -> column mapping (only for the handled branches). Matching is on the
# casefolded warehouse name; used only when BRANCH_STOCK_MAPPING is enabled.
STOCK_RULES: dict[str, dict] = {
    "4489499000001322444": {"mets": ("mets cold storage",), "mets_exclude": ("near-expiry", "for supermarket", "mssi"), "glacier": ("glacier south rgf",)},
    "4489499000017295785": {"mets": ("mets cold storage", "mssi"), "glacier": ("glacier south mssi",)},
}

_RGF_ID, _MSSI_ID = BRANCHES[0]["id"], BRANCHES[1]["id"]
DEFAULT_ALLOWED_BRANCH_IDS = (_RGF_ID, _MSSI_ID)


def _resolve_allowed() -> tuple[str, ...]:
    raw = os.environ.get("ALLOWED_BRANCHES", "")
    ids = tuple(part.strip() for part in raw.split(",") if part.strip())
    return ids or DEFAULT_ALLOWED_BRANCH_IDS


ALLOWED_BRANCH_IDS: tuple[str, ...] = _resolve_allowed()

# A record with no branch_id at all (Zoho list/detail rows always carry one; Neon history rows keep none)
# is judged by its number, and only against the allowlist: RGF numbering (SO..., WM-SO...) counts as RGF,
# MSSI numbering (MS-SO...) as MSSI. Anything else is excluded.
_NUMBER_RGF = re.compile(r"^\s*(?:SO|WM-SO)(?![A-Za-z])", re.I)
_NUMBER_MSSI = re.compile(r"^\s*MS-SO(?![A-Za-z])", re.I)


def _number_is_allowed(number: str) -> bool:
    return bool((_NUMBER_RGF.match(number) and _RGF_ID in ALLOWED_BRANCH_IDS) or (_NUMBER_MSSI.match(number) and _MSSI_ID in ALLOWED_BRANCH_IDS))


MESSAGE_NOT_HANDLED = "This branch isn't handled in RareChain"


class BranchNotAllowed(HTTPException):
    """409 for any read-by-id or write on an order of a branch RareChain does not handle."""

    def __init__(self, number: str | None = None):
        super().__init__(status_code=409, detail=MESSAGE_NOT_HANDLED)
        self.salesorder_number = number


_excluded_lock = threading.Lock()
_excluded_count = 0


def excluded_count() -> int:
    return _excluded_count


def allowed_branch_ids() -> tuple[str, ...]:
    return ALLOWED_BRANCH_IDS


def branch_param() -> str:
    """Value for Zoho's `branch_ids` list parameter (verified to accept several ids in one call)."""
    return ",".join(ALLOWED_BRANCH_IDS)


def _record_of(row_or_record) -> dict:
    record = row_or_record if isinstance(row_or_record, dict) else (getattr(row_or_record, "raw_json", None) or {})
    return record if isinstance(record, dict) else {}


def _number_of(row_or_record) -> str:
    record = _record_of(row_or_record)
    number = record.get("salesorder_number") or record.get("sales_order_number")
    if not number and not isinstance(row_or_record, dict):
        number = getattr(row_or_record, "salesorder_number", None)
    return str(number or "")


def is_allowed(row_or_record) -> bool:
    """True when the order belongs to an allowed branch. A record without a branch id is allowed only
    if its number is RGF/MSSI numbered. Unknown branches are excluded."""
    record = _record_of(row_or_record)
    branch_id = str(record.get("branch_id") or record.get("location_id") or "").strip()
    if branch_id:
        return branch_id in ALLOWED_BRANCH_IDS
    return _number_is_allowed(_number_of(row_or_record))


def filter_allowed(records: list) -> list:
    """Drop disallowed records immediately after a Zoho fetch (before cache, count or hydration)."""
    global _excluded_count
    kept = [r for r in records if is_allowed(r)]
    dropped = len(records) - len(kept)
    if dropped:
        with _excluded_lock:
            _excluded_count += dropped
    return kept


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
    """The allowed branches only (RGF and MSSI by default), in config order."""
    by_id = {b["id"]: dict(b) for b in BRANCHES}
    for extra in seen or []:
        if extra and extra["id"] in ALLOWED_BRANCH_IDS:
            by_id.setdefault(extra["id"], dict(extra))
    return [by_id[i] for i in ALLOWED_BRANCH_IDS if i in by_id]


def parse_ids(value) -> set[str]:
    # Non-str (e.g. an unresolved FastAPI Query default when a route function is called directly) = no filter.
    if not isinstance(value, str):
        return set()
    return {part.strip() for part in value.split(",") if part.strip()}


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def squash_number(value) -> str:
    """Casefolded, alphanumerics only: 'SS SO26-16612', 'ss-so26-16612' and 'SSSO2616612' all match."""
    return _NON_ALNUM.sub("", str(value or "").casefold())
