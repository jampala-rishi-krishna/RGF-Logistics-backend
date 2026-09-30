"""cache Zoho Inventory sales orders

Revision ID: c4e7a1f0b2d3
Revises: b1d9f0a3c2e4
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c4e7a1f0b2d3"
down_revision = "b1d9f0a3c2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sales_orders_cache",
        sa.Column("id", sa.String(length=200), primary_key=True),
        sa.Column("salesorder_number", sa.Text()),
        sa.Column("reference_number", sa.Text()),
        sa.Column("customer_name", sa.Text()),
        sa.Column("order_status", sa.Text()),
        sa.Column("invoice_status", sa.Text()),
        sa.Column("payment_status", sa.Text()),
        sa.Column("shipment_status", sa.Text()),
        sa.Column("order_date", sa.Date()),
        sa.Column("expected_shipment_date", sa.Date()),
        sa.Column("total", sa.Numeric(14, 2)),
        sa.Column("delivery_method", sa.Text()),
        sa.Column("salesperson_name", sa.Text()),
        sa.Column("customer_po_number", sa.Text()),
        sa.Column("billing_address", postgresql.JSONB()),
        sa.Column("shipping_address", postgresql.JSONB()),
        sa.Column("payment_terms_label", sa.Text()),
        sa.Column("mode_of_transport", sa.Text()),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sales_orders_cache")
