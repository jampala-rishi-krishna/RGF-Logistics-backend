"""Separate current Zoho SO snapshots from historical sales_orders."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "u1a2b3c4d5e6"
down_revision = "t0f1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "zoho_so_live_cache",
        sa.Column("salesorder_id", sa.Text(), primary_key=True),
        sa.Column("shipment_date", sa.Date(), nullable=True),
        sa.Column("branch_id", sa.Text(), nullable=True),
        sa.Column("last_modified_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_zoho_so_live_cache_shipment_date", "zoho_so_live_cache", ["shipment_date"])
    op.create_index("ix_zoho_so_live_cache_last_modified_time", "zoho_so_live_cache", ["last_modified_time"])


def downgrade() -> None:
    op.drop_index("ix_zoho_so_live_cache_last_modified_time", table_name="zoho_so_live_cache")
    op.drop_index("ix_zoho_so_live_cache_shipment_date", table_name="zoho_so_live_cache")
    op.drop_table("zoho_so_live_cache")
