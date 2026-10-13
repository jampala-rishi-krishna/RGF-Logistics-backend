"""Form templates for the crew workflow (acknowledge checklist, delivery checkout): loading and answer validation.

Templates are versioned rows (models.trip.FormTemplate + FormTemplateItem). An acknowledgement / checkout stores the id and
version it was filled against, so validating against an older (still existing) version stays possible for offline phones.
No Zoho, no external call.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.trip import KIND_ACKNOWLEDGE, KIND_CHECKOUT, FormTemplate, FormTemplateItem

PhotoCheck = Callable[[int], bool]


# ---- loading -------------------------------------------------------------------------------------------------------


def _item_payload(item: FormTemplateItem) -> dict:
    return {
        "id": item.id, "key": item.key, "label": item.label, "item_type": item.item_type, "required": bool(item.required),
        "options": item.options, "prefill": item.prefill, "show_when": item.show_when, "position": item.position, "app_only": bool(item.app_only),
    }


def _template_payload(db: Session, template: FormTemplate) -> dict:
    items = db.execute(
        select(FormTemplateItem).where(FormTemplateItem.template_id == template.id, FormTemplateItem.active.is_(True)).order_by(FormTemplateItem.position, FormTemplateItem.id)
    ).scalars().all()
    return {"id": template.id, "kind": template.kind, "version": template.version, "name": template.name, "is_draft": bool(template.is_draft), "items": [_item_payload(i) for i in items]}


def load_active(db: Session, kind: str) -> dict | None:
    template = db.execute(
        select(FormTemplate).where(FormTemplate.kind == kind, FormTemplate.active.is_(True)).order_by(FormTemplate.version.desc())
    ).scalars().first()
    return _template_payload(db, template) if template else None


def load_by_id(db: Session, template_id: int, kind: str | None = None) -> dict | None:
    template = db.get(FormTemplate, template_id)
    if template is None or (kind is not None and template.kind != kind):
        return None
    return _template_payload(db, template)


def load_for_kind(db: Session, kind: str, template_id: int | None) -> dict | None:
    """The template an action names (older versions stay valid) or, without an id, the active one."""
    return load_active(db, kind) if template_id is None else load_by_id(db, template_id, kind)


def templates_payload(db: Session, storage_enabled: bool) -> dict:
    return {"photo_storage_enabled": bool(storage_enabled), KIND_ACKNOWLEDGE: load_active(db, KIND_ACKNOWLEDGE), KIND_CHECKOUT: load_active(db, KIND_CHECKOUT)}


# ---- value helpers -------------------------------------------------------------------------------------------------


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _int_id(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number == int(number) else None


def _is_visible(item: dict, by_key: dict[str, dict], values: dict[str, Any], memo: dict[str, bool], trail: tuple = ()) -> bool:
    key = item["key"]
    if key in memo:
        return memo[key]
    rule = item.get("show_when")
    visible = True
    if isinstance(rule, dict) and rule.get("key"):
        ref = by_key.get(rule["key"])
        if ref is None or ref["key"] in trail or ref["key"] == key:
            visible = False
        else:
            visible = _is_visible(ref, by_key, values, memo, (*trail, key)) and values.get(ref["key"]) in (rule.get("in") or [])
    memo[key] = visible
    return visible


def _photo_state(storage_enabled: bool, item: dict, photo_id: Any, photo_ok: PhotoCheck | None, errors: list[dict], visible: bool) -> int | None:
    """Photo / signature items exist only while storage is enabled; then they are required (when marked so) and must exist."""
    if not storage_enabled or not visible:
        return None
    if _blank(photo_id):
        if item["required"]:
            errors.append({"key": item["key"], "message": f"{item['label']}: a photo is required."})
        return None
    pid = _int_id(photo_id)
    if pid is None or (photo_ok is not None and not photo_ok(pid)):
        errors.append({"key": item["key"], "message": f"{item['label']}: the photo was not found for this delivery. Upload it again."})
        return None
    return pid


# ---- acknowledgement -----------------------------------------------------------------------------------------------


def _ack_value(item_type: str, answer: dict) -> Any:
    if item_type == "checkbox":
        return bool(answer.get("checked"))
    if item_type == "count":
        return _number(answer.get("count"))
    if item_type == "photo":
        return answer.get("photo_id")
    text = answer.get("text") if not _blank(answer.get("text")) else answer.get("note")
    return None if _blank(text) else str(text).strip()


def validate_acknowledgement(template: dict, answers: list[dict] | None, storage_enabled: bool, photo_ok: PhotoCheck | None = None) -> tuple[list[dict], list[dict]]:
    """(normalized answers, errors[{key,message}]). answers = [{"item_id","checked","count","text","photo_id","note"}]."""
    items = template["items"]
    by_id = {item["id"]: item for item in items}
    by_key = {item["key"]: item for item in items}
    errors: list[dict] = []
    given: dict[int, dict] = {}
    for answer in answers or []:
        item_id = _int_id(answer.get("item_id")) if isinstance(answer, dict) else None
        if item_id is None or item_id not in by_id:
            errors.append({"key": f"item_{answer.get('item_id') if isinstance(answer, dict) else answer}", "message": "Unknown checklist item."})
            continue
        if item_id in given:
            errors.append({"key": by_id[item_id]["key"], "message": "This checklist item was answered twice."})
            continue
        given[item_id] = answer

    values = {item["key"]: _ack_value(item["item_type"], given.get(item["id"], {})) for item in items}
    memo: dict[str, bool] = {}
    normalized: list[dict] = []
    for item in items:
        visible = _is_visible(item, by_key, values, memo)
        answer = given.get(item["id"], {})
        kind, label, required = item["item_type"], item["label"], item["required"]
        out = {"item_id": item["id"], "key": item["key"], "label": label, "type": kind, "checked": None, "count": None, "text": None, "photo_id": None, "note": None}
        if not visible:
            continue
        if kind == "checkbox":
            out["checked"] = bool(answer.get("checked"))
            if required and not out["checked"]:
                errors.append({"key": item["key"], "message": f"{label} must be checked."})
        elif kind == "count":
            raw = answer.get("count")
            number = _number(raw)
            if not _blank(raw) and (number is None or number < 0):
                errors.append({"key": item["key"], "message": f"{label} must be a number, 0 or more."})
            elif _blank(raw) and required:
                errors.append({"key": item["key"], "message": f"{label} is required."})
            out["count"] = number
        elif kind == "photo":
            out["photo_id"] = _photo_state(storage_enabled, item, answer.get("photo_id"), photo_ok, errors, True)
        else:  # note / text
            text = values[item["key"]]
            out["text"] = answer.get("text") if not _blank(answer.get("text")) else None
            out["note"] = answer.get("note") if not _blank(answer.get("note")) else None
            if required and text is None:
                errors.append({"key": item["key"], "message": f"{label} is required."})
        if out["note"] is None and kind != "note" and not _blank(answer.get("note")):
            out["note"] = str(answer["note"]).strip()
        normalized.append(out)
    return normalized, errors


# ---- checkout ------------------------------------------------------------------------------------------------------


def _parse_date(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        return None


def _parse_datetime(value: Any) -> str | None:
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def validate_checkout(template: dict, answers: dict | None, storage_enabled: bool, photo_ok: PhotoCheck | None = None, prefill: dict | None = None) -> tuple[list[dict], list[dict]]:
    """(normalized [{key,label,type,value}], errors[{key,message}]). answers is keyed by item key. Items with a `prefill`
    take the server's value (so_number, customer, ...) over whatever the phone sent."""
    items = template["items"]
    by_key = {item["key"]: item for item in items}
    errors: list[dict] = []
    raw: dict[str, Any] = dict(answers) if isinstance(answers, dict) else {}
    for key in raw:
        if key not in by_key:
            errors.append({"key": str(key), "message": "Unknown field."})
    for item in items:
        server_value = (prefill or {}).get(item["key"]) if item.get("prefill") else None
        if not _blank(server_value):
            raw[item["key"]] = server_value
    memo: dict[str, bool] = {}
    normalized: list[dict] = []
    for item in items:
        if not _is_visible(item, by_key, raw, memo):
            continue
        key, kind, label, required = item["key"], item["item_type"], item["label"], item["required"]
        value = raw.get(key)
        result: Any = None
        if kind in ("photo", "signature"):
            result = _photo_state(storage_enabled, item, value, photo_ok, errors, True)
            if result is None:
                continue  # hidden (storage disabled) or missing: not stored
        elif kind == "checkbox":
            result = value is True
            if value is not None and not isinstance(value, bool):
                errors.append({"key": key, "message": f"{label} must be yes or no."})
            elif required and not result:
                errors.append({"key": key, "message": f"{label} must be checked."})
        elif _blank(value):
            if required:
                errors.append({"key": key, "message": f"{label} is required."})
            continue
        elif kind in ("text", "textarea"):
            if not isinstance(value, str):
                errors.append({"key": key, "message": f"{label} must be text."})
                continue
            result = value.strip()
        elif kind in ("dropdown", "radio"):
            options = [str(o) for o in (item.get("options") or [])]
            if str(value) not in options:
                errors.append({"key": key, "message": f"{label}: choose one of {', '.join(options)}."})
                continue
            result = str(value)
        elif kind == "numeric_text":  # digits only, kept as text so leading zeros survive (Invoice / DR numbers)
            text = str(value).strip()
            if not (text.isascii() and text.isdigit() and len(text) <= 30):
                errors.append({"key": key, "message": f"{label} must be a number (digits only)."})
                continue
            result = text
        elif kind == "number":
            result = _number(value)
            if result is None:
                errors.append({"key": key, "message": f"{label} must be a number."})
                continue
        elif kind == "date":
            result = _parse_date(value)
            if result is None:
                errors.append({"key": key, "message": f"{label} must be a date (YYYY-MM-DD)."})
                continue
        elif kind == "datetime":
            result = _parse_datetime(value)
            if result is None:
                errors.append({"key": key, "message": f"{label} must be a date and time."})
                continue
        else:
            errors.append({"key": key, "message": f"{label}: unsupported field type."})
            continue
        normalized.append({"key": key, "label": label, "type": kind, "value": result})
    return normalized, errors
