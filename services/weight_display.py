"""How a kg figure is written wherever a person reads it (driver app, Driver Overview, emails, Load Planning).

A total built from lines where some weights are unknown is a LOWER BOUND, never the real total. It is written "354 kg+"
with a small note "1 line without weight"; it is never printed as if it were complete. If nothing is known: a dash.
"""
from __future__ import annotations

DASH = "\u2014"


def unknown_note(unknown_lines: int) -> str | None:
    if unknown_lines <= 0:
        return None
    return "1 line without weight" if unknown_lines == 1 else f"{unknown_lines} lines without weight"


def format_kg(kg: float | None, unknown_lines: int = 0, *, decimals: int = 1, unit: bool = True) -> tuple[str, str | None]:
    """(text, note). Complete -> ("354.0 kg", None). Partial -> ("354.0 kg+", "1 line without weight"). Nothing known -> (dash, note)."""
    note = unknown_note(unknown_lines)
    if kg is None or (note is not None and kg <= 0):
        return DASH, note
    text = f"{kg:,.{decimals}f}"
    if note is not None:
        return f"{text} kg+", note
    return f"{text} kg" if unit else text, None
