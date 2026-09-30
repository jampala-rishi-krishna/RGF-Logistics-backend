
from __future__ import annotations

import logging
import os
import ssl
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv
from sqlalchemy import BigInteger, DateTime, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

logger = logging.getLogger("db_egress")

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

# Switch from psycopg/psycopg2 to the pure-Python pg8000 driver.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgresql://",
        "postgresql+pg8000://",
        1,
    )

# pg8000 does not accept PostgreSQL's psycopg-specific
# sslmode and channel_binding URL parameters.
parts = urlsplit(DATABASE_URL)
query = dict(parse_qsl(parts.query, keep_blank_values=True))
query.pop("sslmode", None)
query.pop("channel_binding", None)

DATABASE_URL = urlunsplit(
    (
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query),
        parts.fragment,
    )
)

# Neon requires an encrypted PostgreSQL connection.
ssl_context = ssl.create_default_context()

engine = create_engine(
    DATABASE_URL,
    connect_args={"ssl_context": ssl_context, "timeout": 10},
    pool_pre_ping=True,
    pool_recycle=300,
)

@event.listens_for(engine, "connect")
def set_statement_timeout(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET statement_timeout = 30000")
    finally:
        cursor.close()


# [DB_EGRESS] - one counter per statement actually sent to Neon, so egress reductions
# (in-memory GPS store, Fleet cache, etc.) can be verified from /health instead of guessed.
_egress_lock = threading.Lock()
db_egress_stats = {"query_count": 0, "started_at": time.time()}


@event.listens_for(engine, "before_cursor_execute")
def _count_db_egress(conn, cursor, statement, parameters, context, executemany):
    with _egress_lock:
        db_egress_stats["query_count"] += 1
    if db_egress_stats["query_count"] % 500 == 0:
        elapsed = time.time() - db_egress_stats["started_at"]
        logger.info("[DB_EGRESS] %d statements sent to Neon (%.1f/s avg)", db_egress_stats["query_count"], db_egress_stats["query_count"] / elapsed if elapsed else 0)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampedBase(Base):
    __abstract__ = True

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
