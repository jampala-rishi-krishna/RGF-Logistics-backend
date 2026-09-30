from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import TimestampedBase


class Conversation(TimestampedBase):
    """Used by routers/agent.py for the AI chat assistant's conversation history - NOT part
    of routers/comms.py's retired simulated-messaging module (see that module's comment)."""

    __tablename__ = "conversations"

    subject_type: Mapped[str | None] = mapped_column(Text)
    subject_id: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)


class NotificationTemplate(TimestampedBase):
    __tablename__ = "notification_templates"

    name: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    variables_json: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(Text)


class Message(TimestampedBase):
    """Used by routers/agent.py for the AI chat assistant's conversation history - NOT part
    of routers/comms.py's retired simulated-messaging module (see that module's comment)."""

    __tablename__ = "messages"

    conversation_id: Mapped[str | None] = mapped_column(String(200), index=True)
    direction: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    template_id: Mapped[str | None] = mapped_column(String(200))
    content_ref: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
