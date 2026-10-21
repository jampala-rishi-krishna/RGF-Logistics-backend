"""Check-out outcome "cancelled_by_request": the customer or the sales agent cancelled the delivery at the stop.

Stored on the existing trip_stops row like any other check-out (outcome + reason + form_answers JSON) - no new table or
column. The cancel details are extra answers in form_answers:
  cancel_requested_by   customer | sales_agent
  cancel_requester_name who asked
  reason                chip text (+ optional note), also copied to trip_stops.reason
  cancel_proof          photo id of the proof (message / signed note / screenshot), uploaded through the normal
                        /api/media proof path (Google Drive) with field_key "cancel_proof"; mandatory.
"""
from __future__ import annotations

from typing import Any, Callable

CANCELLED_OUTCOME = "cancelled_by_request"
CANCELLED_LABEL = "Cancelled by request"
CANCELLED_SHORT = "Cancelled"
PROOF_KEY = "cancel_proof"
REQUESTERS = {"customer": "Customer", "sales_agent": "Sales agent"}
CANCEL_KEYS = ("cancel_requested_by", "cancel_requester_name", PROOF_KEY, "reason", "cancel_note")


def is_cancel(answers: dict | None) -> bool:
    return isinstance(answers, dict) and str(answers.get("outcome") or "").strip().lower() == CANCELLED_OUTCOME


def is_cancelled(outcome: str | None) -> bool:
    return str(outcome or "").strip().lower() == CANCELLED_OUTCOME


def template_without_outcome(template: dict) -> dict:
    """The check-out template minus its outcome question: every item shown only for an outcome (delivery photo, received by,
    the partial / failed reason) becomes hidden, the rest (invoice / DR, required extras) is validated as usual."""
    return {**template, "items": [item for item in template["items"] if item["key"] != "outcome"]}


def split(sent: dict) -> tuple[dict, dict]:
    """(answers for the template validation, the cancel fields)."""
    rest = {k: v for k, v in sent.items() if k not in CANCEL_KEYS and k != "outcome"}
    return rest, {k: sent.get(k) for k in CANCEL_KEYS}


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _photo_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def validate(raw: dict, storage_enabled: bool, photo_ok: Callable[[int], bool] | None) -> tuple[list[dict], list[dict]]:
    """(normalized answers [{key,label,type,value}] incl. the outcome, errors[{key,message}])."""
    errors: list[dict] = []
    who = _text(raw.get("cancel_requested_by"))
    who = who.lower() if who else None
    name = _text(raw.get("cancel_requester_name"))
    reason = _text(raw.get("reason"))
    note = _text(raw.get("cancel_note"))
    if who not in REQUESTERS:
        errors.append({"key": "cancel_requested_by", "message": "Who asked to cancel: choose Customer or Sales agent."})
    if not name:
        errors.append({"key": "cancel_requester_name", "message": "Name of the person who asked to cancel is required."})
    if not reason:
        errors.append({"key": "reason", "message": "Reason is required."})
    photo = _photo_id(raw.get(PROOF_KEY))
    if not storage_enabled:
        errors.append({"key": PROOF_KEY, "message": "Photo storage is off: a cancellation needs a proof photo. Contact Logistics."})
    elif photo is None:
        errors.append({"key": PROOF_KEY, "message": "Proof photo is required."})
    elif photo_ok is not None and not photo_ok(photo):
        errors.append({"key": PROOF_KEY, "message": "Proof photo was not found. Take the photo again."})
    if errors:
        return [], errors
    full_reason = f"{reason} - {note}" if note and note != reason else reason
    answers = [
        {"key": "outcome", "label": "Delivery outcome", "type": "radio", "value": CANCELLED_OUTCOME},
        {"key": "cancel_requested_by", "label": "Cancel requested by", "type": "text", "value": REQUESTERS[who]},
        {"key": "cancel_requester_name", "label": "Requester name", "type": "text", "value": name},
        {"key": "reason", "label": "Reason", "type": "textarea", "value": full_reason},
        {"key": PROOF_KEY, "label": "Cancellation proof photo", "type": "photo", "value": photo},
    ]
    return answers, []


def details(form_answers: list | None) -> dict:
    """{requested_by, requester_name, reason, proof_photo_id} from a cancelled stop's stored answers."""
    values = {a.get("key"): a.get("value") for a in form_answers or [] if isinstance(a, dict)}
    return {"requested_by": values.get("cancel_requested_by"), "requester_name": values.get("cancel_requester_name"),
            "reason": values.get("reason"), "proof_photo_id": _photo_id(values.get(PROOF_KEY))}
