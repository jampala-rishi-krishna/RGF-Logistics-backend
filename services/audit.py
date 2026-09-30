from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

_lock = threading.Lock()
_entries: list[dict] = []


def write_audit_log(
    db: Any = None,
    *,
    actor_id: str | None,
    action: str,
    target_entity: str,
    target_id: Any,
    details: dict | None = None,
) -> None:
    """2026-09-24 (Step 5): audit_log dropped from Neon - in-memory only now. `db` is kept
    as an accepted-but-unused first positional arg so every existing call site
    (alerts.py/optimization.py/agent.py) needs zero changes."""
    with _lock:
        _entries.append({
            "actor_id": actor_id or "",
            "action": action,
            "target_entity": target_entity,
            "target_id": str(target_id),
            "event_time": datetime.now(timezone.utc),
            "details": details or {},
        })


def list_audit_log(target_entity: str | None = None, actor_id: str | None = None) -> list[dict]:
    with _lock:
        rows = list(_entries)
    if target_entity:
        rows = [r for r in rows if r["target_entity"] == target_entity]
    if actor_id:
        rows = [r for r in rows if r["actor_id"] == actor_id]
    return rows
