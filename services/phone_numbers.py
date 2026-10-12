"""Philippine mobile number normalisation for login and the users table."""
from __future__ import annotations

import re


def ph_mobile_core(raw: str | None) -> str | None:
    """The 10 digits after the country/trunk prefix (9XXXXXXXXX) for 09xxxxxxxxx / +639xxxxxxxxx / 639xxxxxxxxx /
    9xxxxxxxxx, else None."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits.startswith("09"):
        digits = digits[1:]
    elif len(digits) == 12 and digits.startswith("639"):
        digits = digits[2:]
    if len(digits) == 10 and digits.startswith("9"):
        return digits
    return None


def ph_phone_digits(raw: str | None) -> int | None:
    """639XXXXXXXXX as an int - what users.phone (BigInteger) stores for accounts created by the driver seed."""
    core = ph_mobile_core(raw)
    return int(f"63{core}") if core else None


def ph_phone_lookup_values(raw: str | None) -> list[int]:
    """Every integer form an existing users.phone might hold for this number (639XXXXXXXXX, or 9XXXXXXXXX when the
    leading digits were lost). Empty when `raw` is not a PH mobile number."""
    core = ph_mobile_core(raw)
    return [int(f"63{core}"), int(core)] if core else []
