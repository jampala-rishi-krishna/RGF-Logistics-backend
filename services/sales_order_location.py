from __future__ import annotations

import json
import re
import unicodedata


def address_object(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    if isinstance(value, list):
        value = value[0] if value else {}
    return value if isinstance(value, dict) else {}


def address_lines(value, include_names: bool = True) -> list[str]:
    """Every meaningful part of a Zoho address, in reading order. Zoho keeps the street in
    `street2` and sometimes only a name in `address`, so no field can be skipped."""
    if isinstance(value, str) and not address_object(value):
        text = value.strip()
        return [text] if text else []
    item = address_object(value)

    def part(key: str) -> str:
        found = item.get(key)
        return "" if found is None else str(found).strip()

    locality = ", ".join(p for p in (part("city"), part("state"), part("zip")) if p)
    seen: set[str] = set()
    lines = []
    names = (part("company_name"), part("attention")) if include_names else ()
    for line in (*names, part("address"), part("street_address"), part("street2"), locality, part("country")):
        if line and line.lower() not in seen:
            seen.add(line.lower())
            lines.append(line)
    return lines


def find_city(value) -> str | None:
    if isinstance(value, dict):
        for key in ("city", "City", "town", "municipality"):
            if value.get(key):
                return str(value[key]).strip()
        for child in value.values():
            found = find_city(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_city(child)
            if found:
                return found
    elif isinstance(value, str):
        try:
            return find_city(json.loads(value))
        except (TypeError, ValueError):
            return None
    return None


# ---- City inference ------------------------------------------------------------------
# Some Zoho orders leave the city field blank and put it in the street text instead (e.g.
# street2 = "220 Pilar St., Brgy. Addition Hills Mandaluyong City"). Only when the city field is
# empty do we look for a known Philippine city written with an explicit "City" suffix.
KNOWN_CITIES = (
    "Caloocan City", "Las Piñas City", "Makati City", "Malabon City", "Mandaluyong City", "Manila City", "Marikina City",
    "Muntinlupa City", "Navotas City", "Parañaque City", "Pasay City", "Pasig City", "Quezon City", "San Juan City",
    "Taguig City", "Valenzuela City", "Antipolo City", "Bacoor City", "Biñan City", "Cabuyao City", "Calamba City",
    "Carmona City", "Cavite City", "Dasmariñas City", "General Trias City", "Imus City", "San Pedro City", "Santa Rosa City",
    "Tagaytay City", "Trece Martires City", "Batangas City", "Lipa City", "Tanauan City", "Lucena City", "Meycauayan City",
    "Malolos City", "San Jose del Monte City", "Angeles City", "San Fernando City", "Olongapo City", "Tarlac City",
    "Cabanatuan City", "Baguio City", "Naga City", "Legazpi City", "Cebu City", "Lapu-Lapu City", "Mandaue City",
    "Talisay City", "Iloilo City", "Bacolod City", "Dumaguete City", "Tacloban City", "Davao City", "Cagayan de Oro City",
    "Zamboanga City", "General Santos City", "Butuan City",
)
_learned_cities: dict[str, str] = {}


def _fold(text: str) -> str:
    stripped = "".join(ch for ch in unicodedata.normalize("NFKD", str(text)) if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).casefold().strip()


def _learn_city(city: str | None) -> None:
    """Remember real city values Zoho gives us so other blank-city orders can use them."""
    if city and len(city) > 3:
        _learned_cities.setdefault(_fold(city), city.strip())


def infer_city(value) -> str | None:
    text = _fold(" ".join(address_lines(value, include_names=False)))
    if not text:
        return None
    candidates = {_fold(name): name for name in KNOWN_CITIES}
    candidates.update({key: name for key, name in _learned_cities.items() if key.endswith(" city")})
    for folded in sorted(candidates, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(folded) + r"(?![a-z])", text):
            return candidates[folded]
    return None


def shipping_city(row) -> str | None:
    # Historical sales-order rows intentionally have no raw_json.  Keep this
    # resolver usable for both live Zoho cache objects and durable history rows.
    raw = getattr(row, "raw_json", None) or {}
    address = address_object(row.shipping_address or raw.get("shipping_address"))
    if not address:
        address = address_object(raw.get("shipping_address_details") or raw.get("customer", {}).get("shipping_address"))
    found = find_city(address) or find_city(raw.get("shipping_address") or raw)
    if found:
        _learn_city(found)
        return found
    return infer_city(address or raw.get("shipping_address"))
