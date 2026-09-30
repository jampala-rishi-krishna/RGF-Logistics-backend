"""Finalize persisted sales-order history and extract line items.

The live Zoho path remains in memory.  This migration only changes the durable
write-on-event history table and preserves a reversible downgrade for the
history/line-item changes.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f6b7c8d9e0a1"
down_revision = "e5a2f9c3d7b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("sales_order_history", "sales_orders")
    op.add_column("sales_orders", sa.Column("delivery_status", sa.Text(), nullable=True))
    op.add_column("sales_orders", sa.Column("is_reefer", sa.Boolean(), nullable=True))

    op.create_table(
        "sales_order_lines",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sales_order_id", sa.String(200), nullable=False),
        sa.Column("item_id", sa.String(200), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("sku", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("quantity_shipped", sa.Float(), nullable=True),
        sa.Column("weight_kg", sa.Float(), nullable=True),
        sa.Column("location_name", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["sales_order_id"], ["sales_orders.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_sales_order_lines_sales_order_id", "sales_order_lines", ["sales_order_id"])

    # Backfill persisted line items before raw_json is removed.  The JSON shape
    # is intentionally defensive because Zoho has emitted both item_id and id.
    op.execute(sa.text("""
        INSERT INTO sales_order_lines
            (sales_order_id, item_id, name, sku, quantity, unit, quantity_shipped,
             weight_kg, location_name, created_at, updated_at)
        SELECT
            s.id,
            COALESCE(item->>'item_id', item->>'itemid', item->'item'->>'item_id', item->'item'->>'id'),
            COALESCE(item->>'name', item->>'item_description', item->>'description'),
            COALESCE(item->>'sku', item->>'item_order', item->>'item_id'),
            NULLIF(COALESCE(item->>'quantity', '0'), '')::double precision,
            COALESCE(item->>'unit', item->>'unit_name', item->>'usage_unit'),
            NULLIF(COALESCE(item->>'quantity_shipped', '0'), '')::double precision,
            NULL,
            COALESCE(item->>'location_name', item->>'warehouse_name'),
            now(), now()
        FROM sales_orders s
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(s.raw_json->'line_items', '[]'::jsonb)) AS item
    """))
    op.drop_column("sales_orders", "raw_json")


def downgrade() -> None:
    op.add_column("sales_orders", sa.Column("raw_json", postgresql.JSONB(), nullable=True, server_default="{}"))
    op.execute(sa.text("UPDATE sales_orders SET raw_json = '{}'::jsonb WHERE raw_json IS NULL"))
    op.alter_column("sales_orders", "raw_json", nullable=False, server_default=None)
    op.drop_index("ix_sales_order_lines_sales_order_id", table_name="sales_order_lines")
    op.drop_table("sales_order_lines")
    op.drop_column("sales_orders", "is_reefer")
    op.drop_column("sales_orders", "delivery_status")
    op.rename_table("sales_orders", "sales_order_history")
