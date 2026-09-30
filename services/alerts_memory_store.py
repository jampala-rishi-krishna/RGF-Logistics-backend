from __future__ import annotations

import threading
from datetime import datetime, timezone

from services.audit import write_audit_log

_lock = threading.Lock()
_alerts: dict[int, dict] = {}
_escalations: list[dict] = []
_next_id = 1


def list_alerts(status: str | None = None, severity: str | None = None) -> list[dict]:
    with _lock:
        rows = list(_alerts.values())
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if severity:
        rows = [r for r in rows if r.get("severity") == severity]
    return rows


def get_alert(alert_id: int) -> dict | None:
    with _lock:
        return _alerts.get(alert_id)


def _write_audit(actor_id: str, action: str, target_id: int, details: dict) -> None:
    write_audit_log(actor_id=actor_id, action=action, target_entity="alerts", target_id=target_id, details=details)


def acknowledge(alert_id: int, actor_id: str) -> dict | None:
    with _lock:
        alert = _alerts.get(alert_id)
        if alert is None:
            return None
        previous = alert.get("status")
        alert["status"] = "acknowledged"
        _write_audit(actor_id, "acknowledge", alert_id, {"previousStatus": previous})
        return dict(alert)


def escalate(alert_id: int, actor_id: str, escalated_to_user_id: str, note: str) -> dict | None:
    with _lock:
        alert = _alerts.get(alert_id)
        if alert is None:
            return None
        previous = alert.get("status")
        alert["status"] = "escalated"
        _escalations.append({"alert_id": alert_id, "escalated_to_user_id": escalated_to_user_id, "escalated_at": datetime.now(timezone.utc), "resolution_note": note})
        _write_audit(actor_id, "escalate", alert_id, {"previousStatus": previous, "escalatedToUserId": escalated_to_user_id})
        return dict(alert)


def resolve(alert_id: int, actor_id: str, note: str) -> dict | None:
    with _lock:
        alert = _alerts.get(alert_id)
        if alert is None:
            return None
        previous = alert.get("status")
        alert["status"] = "resolved"
        alert["resolved_at"] = datetime.now(timezone.utc)
        _write_audit(actor_id, "resolve", alert_id, {"previousStatus": previous, "note": note})
        return dict(alert)


def create_alert(**fields) -> dict:
    """No live caller creates alerts today (they were seed-only, see scripts/seed_demo.py) -
    kept so a future trigger (e.g. a monitoring job) has somewhere to write without a DB."""
    global _next_id
    with _lock:
        alert = {"id": _next_id, "status": "open", "created_at": datetime.now(timezone.utc), **fields}
        _alerts[_next_id] = alert
        _next_id += 1
        return dict(alert)
