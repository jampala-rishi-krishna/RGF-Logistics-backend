"""Undo UTF-8 text that was decoded as Windows-1252 ("Denny's" with a curly apostrophe arrives as "DennyAE..." and the
middle dot arrives as "A-circumflex + middle dot"). One implementation, used at every ingestion point (Zoho Sales Orders, the n8n
staff directory) and as a last-mile guard on driver-app strings.

The escapes below keep this file pure ASCII, so no editor, console or encoding setting can corrupt the patterns themselves.
"""
from __future__ import annotations

import re

_MOJIBAKE = re.compile("[\u00c2\u00c3\u00e2][\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc\u2013-\u203a\u20ac\u2122]")


def repair_text(value: str) -> str:
    if not _MOJIBAKE.search(value):
        return value
    for codec in ("cp1252", "latin-1"):
        try:
            fixed = value.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != value:
            return fixed
    return value


def repair_deep(value):
    if isinstance(value, str):
        return repair_text(value)
    if isinstance(value, list):
        return [repair_deep(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_deep(item) for key, item in value.items()}
    return value
