"""Driver/helper trip workflow tables (migration o5a6b7c8d9e0, pre-approved by Rishi 2026-10-12).

One TRIP per assignment batch (sales_orders.assignment_batch_id). Every crew / officer action is ONE write (plus its
append-only trip_events row). GPS is stored as JSON {"lat","lng","accuracy_m","reason"} (lat/lng null + reason when the
phone denied location). Timestamps: *_at = the phone's action time (client_ts), *_received_at = when the server got it.
No blobs in Neon: photos / signatures live in object storage (R2); trip_photos holds only their keys.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase

KIND_ACKNOWLEDGE = "acknowledge_checklist"
KIND_CHECKOUT = "delivery_checkout"
TRIP_STATUSES = ("pending", "on_trip", "returned", "handover_submitted", "closed")
STOP_STATUSES = ("pending", "checked_in", "checked_out")


class FormTemplate(TimestampedBase):
    """Admin-editable form definition. Editing never mutates a version: it creates version N+1 and activates it, so every
    acknowledgement / checkout keeps pointing at the exact template it was filled with."""

    __tablename__ = "form_templates"
    __table_args__ = (UniqueConstraint("kind", "version", name="uq_form_templates_kind_version"),)

    kind: Mapped[str] = mapped_column(Text, nullable=False, index=True)  # acknowledge_checklist | delivery_checkout
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # shown as "DRAFT" in the UI
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # exactly one active version per kind
    source_note: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(BigInteger)


class FormTemplateItem(TimestampedBase):
    __tablename__ = "form_template_items"
    __table_args__ = (UniqueConstraint("template_id", "key", name="uq_form_template_items_template_key"),)

    template_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("form_templates.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)  # stable machine key, e.g. "products_match"
    label: Mapped[str] = mapped_column(Text, nullable=False)
    # checkbox | count | photo | note  (checklist);  also text | textarea | dropdown | radio | number | date | datetime | signature (checkout)
    item_type: Mapped[str] = mapped_column(Text, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    options: Mapped[list | None] = mapped_column(JSONB)  # choices for dropdown / radio
    prefill: Mapped[str | None] = mapped_column(Text)  # so_number | customer | address | driver | vehicle | helper | date
    show_when: Mapped[dict | None] = mapped_column(JSONB)  # {"key": "outcome", "in": ["Partially delivered", ...]}; required only while visible
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # True = a field the app asks for that is NOT in the Zoho form (Settings shows "added in app, not in Zoho form"; Logistics may turn it off).
    app_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Trip(TimestampedBase):
    __tablename__ = "trips"

    batch_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", index=True)  # TRIP_STATUSES
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)  # Manila shipment day of the batch
    vehicle: Mapped[str | None] = mapped_column(Text)
    driver_staff_id: Mapped[int | None] = mapped_column(Integer, index=True)
    helper_staff_ids: Mapped[list | None] = mapped_column(JSONB)  # snapshot at creation (driver excluded)
    warehouse: Mapped[str | None] = mapped_column(Text)  # METS | GLACIER snapshot (from the driver's staff record)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    started_gps: Mapped[dict | None] = mapped_column(JSONB)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    ended_gps: Mapped[dict | None] = mapped_column(JSONB)
    end_distance_m: Mapped[float | None] = mapped_column(Float)  # from the warehouse, when both coordinates are known
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set when the officer validated every invoice
    stop_order_source: Mapped[str | None] = mapped_column(Text)  # optimized | assignment


class SoAcknowledgement(TimestampedBase):
    """Checklist completed = SO acknowledged = product + documents are in the crew's custody."""

    __tablename__ = "so_acknowledgements"
    __table_args__ = (UniqueConstraint("trip_id", "so_id", name="uq_so_ack_trip_so"),)

    trip_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    so_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    template_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("form_templates.id"), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    answers: Mapped[list] = mapped_column(JSONB, nullable=False)  # [{"item_id","key","checked","count","text","photo_id","note"}]
    discrepancy_note: Mapped[str | None] = mapped_column(Text)
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    by_role: Mapped[str] = mapped_column(Text, nullable=False)
    by_staff_id: Mapped[int | None] = mapped_column(Integer)
    gps: Mapped[dict | None] = mapped_column(JSONB)


class TripStop(TimestampedBase):
    """One row per SO of the batch, created when the trip starts (sequence frozen then)."""

    __tablename__ = "trip_stops"
    __table_args__ = (UniqueConstraint("trip_id", "so_id", name="uq_trip_stops_trip_so"),)

    trip_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False, index=True)
    so_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    so_number: Mapped[str | None] = mapped_column(Text)
    customer_name: Mapped[str | None] = mapped_column(Text)
    address_text: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Float)  # only when the SO address carries coordinates
    lng: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")  # STOP_STATUSES
    checkin_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkin_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkin_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    checkin_gps: Mapped[dict | None] = mapped_column(JSONB)
    checkin_distance_m: Mapped[float | None] = mapped_column(Float)
    checkin_far: Mapped[bool | None] = mapped_column(Boolean)  # > 500 m from the address: warning only
    checkout_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkout_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkout_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    checkout_gps: Mapped[dict | None] = mapped_column(JSONB)
    outcome: Mapped[str | None] = mapped_column(Text)  # Delivered | Partially delivered | Failed / Returned (per template options)
    reason: Mapped[str | None] = mapped_column(Text)
    form_template_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("form_templates.id"))
    form_template_version: Mapped[int | None] = mapped_column(Integer)
    form_answers: Mapped[list | None] = mapped_column(JSONB)  # [{"key","label","value"}] (photo / signature values are photo ids)


class TripEvent(TimestampedBase):
    """Append-only audit log + idempotency ledger. `result` is the response of the first execution so a retried request
    (same idempotency_key) returns exactly what the first one returned."""

    __tablename__ = "trip_events"

    trip_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)  # acknowledged | trip_started | checked_in | checked_out | trip_ended | handover_submitted | officer_acknowledged | invoice_validated | invoice_resolved | trip_closed | assignment_transferred
    so_id: Mapped[str | None] = mapped_column(Text, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    staff_id: Mapped[int | None] = mapped_column(Integer)
    client_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    gps: Mapped[dict | None] = mapped_column(JSONB)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)


class TripPhoto(TimestampedBase):
    """Metadata of a photo / signature held in object storage (never the bytes)."""

    __tablename__ = "trip_photos"

    batch_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    so_id: Mapped[str | None] = mapped_column(Text)
    field_key: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    upload_key: Mapped[str | None] = mapped_column(Text, unique=True)  # client-generated: a retried upload is not stored twice


class InvoiceHandover(TimestampedBase):
    """Crew -> officer hand-over of the signed carbon copies of one trip."""

    __tablename__ = "invoice_handovers"

    trip_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False, unique=True)
    batch_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", index=True)  # pending | acknowledged (officer saw it, issues remain) | closed
    submitted_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_gps: Mapped[dict | None] = mapped_column(JSONB)
    officer_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # the officer the crew chose to receive it
    officer_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    officer_acknowledged_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(Text)


class InvoiceHandoverItem(TimestampedBase):
    __tablename__ = "invoice_handover_items"
    __table_args__ = (UniqueConstraint("handover_id", "so_id", name="uq_handover_items_handover_so"),)

    handover_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("invoice_handovers.id", ondelete="CASCADE"), nullable=False, index=True)
    so_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    so_number: Mapped[str | None] = mapped_column(Text)
    customer_name: Mapped[str | None] = mapped_column(Text)
    handed_over: Mapped[bool] = mapped_column(Boolean, nullable=False)  # crew: signed carbon copy handed over yes/no
    crew_note: Mapped[str | None] = mapped_column(Text)  # lost, not signed, customer kept it, ...
    officer_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", index=True)  # pending | received | missing | discrepancy
    officer_note: Mapped[str | None] = mapped_column(Text)
    officer_user_id: Mapped[int | None] = mapped_column(BigInteger)
    officer_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set when a missing / discrepancy item is later marked received
    invoice_no: Mapped[str | None] = mapped_column(Text)  # the Invoice / DR number typed at check-out: the officer's carbon-copy line is "SO number + this"


ISSUE_CATEGORIES = (
    "Delivery delay", "Customer unavailable", "Incorrect address", "Damaged goods", "Missing goods", "Partial delivery", "Vehicle breakdown", "Traffic or route issue",
    "Incorrect assignment", "Vehicle mismatch", "Helper unavailable", "Refrigeration or cargo handling", "Application or technical issue", "Other",
)
ISSUE_PRIORITIES = ("normal", "high", "urgent")
ISSUE_STATUSES = ("submitted", "acknowledged", "in_progress", "resolved", "closed")


class DriverIssue(TimestampedBase):
    """An issue a driver / helper reports from a trip, an order or their profile. Visible to dispatch in Driver Overview > Issues."""

    __tablename__ = "driver_issues"

    reference: Mapped[str | None] = mapped_column(Text, unique=True)  # IS-YYYYMMDD-0007, set right after the insert
    reporter_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reporter_staff_id: Mapped[int | None] = mapped_column(Integer, index=True)
    reporter_role: Mapped[str] = mapped_column(Text, nullable=False)
    batch_id: Mapped[str | None] = mapped_column(Text, index=True)
    so_id: Mapped[str | None] = mapped_column(Text)
    so_number: Mapped[str | None] = mapped_column(Text)
    vehicle: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(Text, nullable=False, default="normal")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    photo_ids: Mapped[list | None] = mapped_column(JSONB)  # trip_photos ids (the bytes live in R2)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="submitted", index=True)
    response: Mapped[str | None] = mapped_column(Text)  # dispatch's reply, shown to the driver
    responded_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)


class DriverAlertRead(TimestampedBase):
    """Read marker of one derived alert (the alerts are computed from assignments / trip events / issues, never stored)."""

    __tablename__ = "driver_alert_reads"
    __table_args__ = (UniqueConstraint("user_id", "alert_key", name="uq_driver_alert_reads_user_key"),)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    alert_key: Mapped[str] = mapped_column(Text, nullable=False)
