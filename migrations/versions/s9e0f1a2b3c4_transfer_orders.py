"""Transfer orders in Load Planning (2026-10-13, pre-approved by Rishi). ADDITIVE only: three nullable/defaulted columns on sales_orders.

order_type: 'SO' (every existing row, via the server default) or 'TO'. A TO is stored like an SO row (id 'TO:<zoho id>', number = the TO#,
customer_name = the destination warehouse) so assignment, the driver app, Fleet and the trip workflow work on it unchanged.
source_warehouse / destination_warehouse: the TO's two warehouses (display names), NULL for SO rows.
Existing SO rows and assignment_batch_id are untouched. No new table.
"""
from alembic import op
import sqlalchemy as sa

revision = "s9e0f1a2b3c4"
down_revision = "r8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sales_orders", sa.Column("order_type", sa.String(length=4), nullable=False, server_default="SO"))
    op.add_column("sales_orders", sa.Column("source_warehouse", sa.Text(), nullable=True))
    op.add_column("sales_orders", sa.Column("destination_warehouse", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sales_orders", "destination_warehouse")
    op.drop_column("sales_orders", "source_warehouse")
    op.drop_column("sales_orders", "order_type")
