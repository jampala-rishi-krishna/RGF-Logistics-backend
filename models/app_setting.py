from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class AppSetting(Base):
    """One admin-editable, NON-SECRET setting override (migration p6b7c8d9e0f1). Absent row = use the environment
    variable / built-in default. Read once at startup, written only by Admin > Settings; see services/app_settings.py."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
