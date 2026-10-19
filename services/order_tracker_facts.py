"""Bounded projections of reads the existing trip workflows already perform.

No SQL or provider IO here. Only successful GET requests publish pending snapshots;
transactional action reads cannot expose uncommitted state.
"""
from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from database import get_db


class ByteCache:
    def __init__(self, max_bytes, ttl, max_entries=512):
        self.max_bytes, self.ttl, self.max_entries = max_bytes, ttl, max_entries
        self.items = OrderedDict()
        self.size = 0
        self.lock = threading.RLock()

    def _prune(self):
        now = time.monotonic()
        for key in list(self.items):
            if now - self.items[key][0] >= self.ttl:
                self.size -= len(self.items.pop(key)[1])

    def put(self, key, value):
        packed = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        if len(packed) > min(self.max_bytes, 65536):
            return False
        with self.lock:
            self._prune()
            if key in self.items:
                self.size -= len(self.items.pop(key)[1])
            while self.items and (self.size + len(packed) > self.max_bytes or len(self.items) >= self.max_entries):
                self.size -= len(self.items.popitem(last=False)[1][1])
            self.items[key] = (time.monotonic(), packed)
            self.size += len(packed)
        return True

    def get(self, key):
        with self.lock:
            self._prune()
            entry = self.items.get(key)
            return json.loads(entry[1]) if entry else None

    def clear(self):
        with self.lock:
            self.items.clear()
            self.size = 0


facts = ByteCache(2 * 1024 * 1024, 86400)


def stamp(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
    return str(value)


def queue(db, key, value):
    if not hasattr(db, "info"):
        return  # Lightweight existing reader fakes have no session-local storage.
    packed = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(packed) > 65536:
        return
    pending = db.info.setdefault("order_tracker_reads", {})
    pending.pop(key, None)
    while pending and (len(pending) >= 32 or sum(len(v) for v in pending.values()) + len(packed) > 262144):
        pending.pop(next(iter(pending)))
    pending[key] = packed


def capture(db, batch_ids, data, epoch=None):
    """Queue a projection; the GET dependency publishes only after the handler succeeds."""
    from services import trip_events, live_sales_order_cache
    event_gen, assignment_gen = epoch or (trip_events.generation(), live_sales_order_cache.assignment_generation())
    for batch in batch_ids[:512]:
        trip = data.trips.get(batch)
        trip_id = trip.id if trip else None
        acks, stops = data.acks.get(trip_id, {}), data.stops.get(trip_id, {})
        handover = data.handovers.get(batch)
        projection = {
            "generation": event_gen, "assignment_generation": assignment_gen,
            "as_of": stamp(datetime.now(timezone.utc)),
            "trip": {"status": trip.status, "vehicle": trip.vehicle,
                     "started_at": stamp(trip.started_at), "ended_at": stamp(trip.ended_at)} if trip else {"status": "pending"},
            "acks": {oid: {"at": stamp(ack.acknowledged_at), "by": data.user(ack.by_user_id, ack.by_role),
                           "answers": deepcopy(ack.answers or []), "gps": deepcopy(ack.gps)} for oid, ack in acks.items()},
            "stops": {oid: {"sequence": stop.sequence, "status": stop.status,
                             "checkin_at": stamp(stop.checkin_at), "checkout_at": stamp(stop.checkout_at),
                             "outcome": stop.outcome, "reason": stop.reason,
                             "answers": deepcopy(stop.form_answers or [])} for oid, stop in stops.items()},
            "invoices": {item.so_id: {"number": item.invoice_no, "status": item.officer_status,
                                      "note": item.officer_note, "checked_at": stamp(item.officer_checked_at)}
                         for item in data.handover_items.get(handover.id, [])} if handover else {},
        }
        queue(db, batch, projection)


def publish(db):
    for batch, projection in db.info.pop("order_tracker_reads", {}).items():
        if isinstance(projection, bytes):
            projection = json.loads(projection)
        facts.put(batch, projection)


def publish_existing_reads(request: Request, db: Session = Depends(get_db)):
    yield
    if request.method == "GET":
        publish(db)


def current(batch):
    from services import trip_events, live_sales_order_cache
    value = facts.get(batch)
    if value and value["generation"] == trip_events.generation() and value["assignment_generation"] == live_sales_order_cache.assignment_generation():
        return value
    return None


def capture_issues(db, items):
    from services import trip_events, live_sales_order_cache
    from services.order_tracker import safe_text
    grouped = {}
    for item in items:
        if item.get("so_id"):
            grouped.setdefault(str(item["so_id"]), []).append({k: safe_text(item.get(k), 600) for k in (
                "reference", "status", "category", "description", "response", "created_at", "responded_at")})
    for oid, issues in grouped.items():
        queue(db, "issues|" + oid, {"generation": trip_events.generation(),
                                  "assignment_generation": live_sales_order_cache.assignment_generation(),
                                  "as_of": stamp(datetime.now(timezone.utc)), "issues": issues[:10]})


def capture_loading(manifest_id, checklist):
    """Called only with the payload of the existing manifest checklist workflow."""
    keys = ("cargo_count_verified", "cargo_count_expected", "cargo_count_actual", "seal_number",
            "departure_temp_c", "departure_temp_zone_count", "driver_acknowledged", "checklist_completed", "completed_at")
    facts.put("loading|" + str(manifest_id), {"as_of": stamp(datetime.now(timezone.utc)),
                                           "checklist": {k: checklist.get(k) for k in keys}})


def capture_rules(customer, rules):
    from services.order_tracker import norm, safe_text
    facts.put("rules|" + norm(customer), {"as_of": stamp(datetime.now(timezone.utc)),
              "rules": {k: safe_text(v) if isinstance(v, str) else v for k, v in (rules or {}).items()}})
