"""A response boundary for assignment attribution, including nested raw payloads."""
PRIVATE_KEYS = frozenset({"assigned_by", "assigned_by_name", "assigned_at", "assignedBy", "assignedAt"})


def redact(value, manager=False):
    if isinstance(value, dict):
        return {k: redact(v, manager) for k, v in value.items() if manager or k not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [redact(v, manager) for v in value]
    return value
