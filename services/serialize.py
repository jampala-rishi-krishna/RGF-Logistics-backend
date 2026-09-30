from __future__ import annotations

from typing import Any


def row_to_dict(obj: Any, *, exclude: set[str] = frozenset()) -> dict:
    """Flattens a SQLAlchemy ORM row into a plain dict, ISO-formatting datetimes. Field names
    match the model's column names exactly (id/created_at/updated_at replace Catalyst's
    ROWID/CREATEDTIME/MODIFIEDTIME - see the migration report's Step 1/Step 4 notes)."""
    result = {}
    for column in obj.__table__.columns:
        if column.name in exclude:
            continue
        value = getattr(obj, column.name)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        result[column.name] = value
    return result
