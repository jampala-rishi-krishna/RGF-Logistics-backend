"""Check-out outcome "cancelled_by_request": requester type + name + reason + a proof photo are required; stored once (idempotent);
shown as "Cancelled" and counted as not delivered. Real routes, in-memory SQLite, R2 mocked, no Zoho."""
from __future__ import annotations

from services import cancel_request, day_model, media_storage
from tests.test_full_driver_flow import _isolate_mocked_r2_provider, acknowledge_all, env, post, stamp, upload  # noqa: F401 (fixtures)


def _at_stop(env, so="s1"):
    acknowledge_all(env)
    assert post(env, "driver", "/api/driver/batches/B1/start", stamp()).status_code == 200
    assert post(env, "driver", f"/api/driver/batches/B1/orders/{so}/check-in", stamp()).status_code == 200


def test_cancel_is_rejected_without_requester_name_reason_or_photo(env):
    _at_stop(env)
    bad = post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", stamp(answers={"invoice_dr_number": "000123", "outcome": "cancelled_by_request"}))
    assert bad.status_code == 422
    assert {e["key"] for e in bad.json()["detail"]["errors"]} == {"cancel_requested_by", "cancel_requester_name", "reason", "cancel_proof"}
    no_photo = post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", stamp(answers={
        "invoice_dr_number": "000123", "outcome": "cancelled_by_request", "cancel_requested_by": "customer", "cancel_requester_name": "Ana", "reason": "Customer changed mind"}))
    assert no_photo.status_code == 422 and [e["key"] for e in no_photo.json()["detail"]["errors"]] == ["cancel_proof"]
    wrong_who = post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", stamp(answers={
        "invoice_dr_number": "000123", "outcome": "cancelled_by_request", "cancel_requested_by": "driver", "cancel_requester_name": "Ana", "reason": "x", "cancel_proof": 999}))
    assert {e["key"] for e in wrong_who.json()["detail"]["errors"]} == {"cancel_requested_by", "cancel_proof"}  # 999 is not a photo of this batch


def test_cancel_is_stored_once_with_its_details(env):
    _at_stop(env)
    photo = upload(env, "driver", "s1", "u-cancel-1", field="cancel_proof")
    assert photo.status_code == 200, photo.text
    body = stamp(answers={"invoice_dr_number": "000123", "outcome": "cancelled_by_request", "cancel_requested_by": "sales_agent", "cancel_requester_name": "Mark (agent)",
                          "reason": "Order cancelled by sales", "cancel_note": "re-order next week", "cancel_proof": photo.json()["photo_id"]})
    done = post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", body)
    assert done.status_code == 200, done.text
    stop = next(o for o in done.json()["batch"]["orders"] if o["id"] == "s1")["workflow"]["stop"]
    assert stop["status"] == "checked_out" and stop["outcome"] == "cancelled_by_request" and stop["outcome_label"] == "Cancelled"
    assert stop["reason"] == "Order cancelled by sales - re-order next week"
    assert stop["cancel"] == {"requested_by": "Sales agent", "requester_name": "Mark (agent)", "reason": "Order cancelled by sales - re-order next week", "proof_photo_id": photo.json()["photo_id"]}
    # same idempotency key replayed, and a new key for the same stop: both are no-ops (no second write)
    again = post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", body)
    assert again.status_code == 200
    other = post(env, "driver", "/api/driver/batches/B1/orders/s1/check-out", {**body, "idempotency_key": "another-key-123"})
    assert other.status_code == 200 and other.json()["already_done"] is True
    progress = done.json()["batch"]["progress"]
    assert progress["failed"] == 1 and progress["delivered"] == 0  # counted as not delivered


def test_cancel_outcome_mappings():
    assert day_model.outcome_bucket("cancelled_by_request") == "failed"
    assert media_storage.outcome_kind("cancelled_by_request") == "cancel_proof"
    assert media_storage.upload_kind("cancel_proof") == "cancel_proof"
    assert media_storage.RESULT_LABEL["cancel_proof"] == "Cancelled by request"
    assert cancel_request.is_cancel({"outcome": "cancelled_by_request"}) and not cancel_request.is_cancel({"outcome": "Failed"})
