from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


# StaffDirectory (staff_directory table) dropped 2026-09-24 - the staff directory now lives
# only in n8n's "Logistics Staff Directory" DataTable, read through
# services/staff_directory_cache.py (see migration d4a1c8f6e9b2).


class CustomerContact(TimestampedBase):
    """Customer-side recipients for Dispatch messaging. v1 scope is email-only (Zoho doesn't
    supply usable phone/WhatsApp numbers) - phone/whatsapp_number are carried as nullable so
    they can be populated later without a schema change."""

    __tablename__ = "customer_contacts"

    zoho_customer_id: Mapped[str | None] = mapped_column(String(200), index=True)
    customer_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    whatsapp_number: Mapped[str | None] = mapped_column(Text)


class DispatchMessageTemplate(TimestampedBase):
    """Table name is `message_templates` per the Dispatch Communications Gateway spec - kept as
    a distinct model/table from the pre-existing `notification_templates` (models/comms.py),
    which belongs to the older simulated-delivery comms system and is not part of this build."""

    __tablename__ = "message_templates"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class DispatchMessageLog(TimestampedBase):
    """Table name is `message_log` per the Dispatch Communications Gateway spec - the single
    source of truth for the Communications Gateway timeline, written to by every n8n
    `[DISPATCH] *` send/reply workflow (directly via Postgres, same pattern the AR and Lead Gen
    n8n projects already use) as well as this backend's own send/webhook endpoints."""

    __tablename__ = "message_log"

    audience: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_name: Mapped[str | None] = mapped_column(Text)
    recipient_contact: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    template_name: Mapped[str | None] = mapped_column(Text)
    trigger_event: Mapped[str | None] = mapped_column(Text)
    related_so_number: Mapped[str | None] = mapped_column(Text, index=True)
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
