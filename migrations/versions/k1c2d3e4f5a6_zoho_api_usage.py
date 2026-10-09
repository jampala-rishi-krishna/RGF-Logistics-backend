"""zoho_api_usage: per-day Zoho API call counters (approved by Rishi 2026-10-09).

One row per (day, category, source) - about 20 rows a day. Written only by additive UPSERT from
services/zoho_usage.py (flush every 60s, only when there is a non-zero delta); read once at startup.
"""
from alembic import op
import sqlalchemy as sa

revision = "k1c2d3e4f5a6"
down_revision = "j1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "zoho_api_usage",
        sa.Column("usage_day", sa.Date(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("usage_day", "category", "source"),
    )


def downgrade() -> None:
    op.drop_table("zoho_api_usage")
