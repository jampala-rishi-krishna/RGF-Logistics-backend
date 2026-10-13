"""ONE definition of "this order line is counted in packs", shared by Load Planning, the driver app and Driver Overview.

Zoho stores the unit of a line as a short code: the real data holds `kg`, `pck` and `pc`. The old test was the substring "pack",
which never matched `pck`, so every pack line was counted as a plain unit and packs showed 0 everywhere.

A line with unit `pc` (pieces) is NOT a pack. A quantity in `kg` is a weight, never a pack count.
"""
from __future__ import annotations

import re

PACK_UNITS = frozenset({"pck", "pcks", "pk", "pks", "pkt", "pkts", "pack", "packs", "package", "packages"})


def is_pack_unit(unit: str | None) -> bool:
    normalized = re.sub(r"[^a-z]", "", str(unit or "").casefold())
    return normalized in PACK_UNITS or normalized.startswith("pack")


def pack_total(lines) -> float | None:
    """Packs of an order (or batch) = sum of the quantities of its pack-unit lines.

    None (shown as "-", never 0) when no line is counted in packs: the pack count is then unknown, not zero.
    `lines` are objects/dicts with `unit` and `quantity`.
    """
    total = 0.0
    found = False
    for line in lines:
        unit = line.get("unit") if isinstance(line, dict) else getattr(line, "unit", None)
        quantity = line.get("quantity") if isinstance(line, dict) else getattr(line, "quantity", None)
        if is_pack_unit(unit):
            found = True
            try:
                total += float(quantity or 0)
            except (TypeError, ValueError):
                pass
    return round(total, 2) if found else None
