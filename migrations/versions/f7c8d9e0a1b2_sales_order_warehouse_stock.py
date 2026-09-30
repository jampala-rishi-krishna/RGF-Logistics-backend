"""Add historical warehouse available-for-sale snapshots to sales_orders."""
from alembic import op
import sqlalchemy as sa

revision = "f7c8d9e0a1b2"
down_revision = "f6b7c8d9e0a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sales_orders", sa.Column("mets_qty_available_for_sale", sa.Numeric(14, 3), nullable=True))
    op.add_column("sales_orders", sa.Column("glacier_qty_available_for_sale", sa.Numeric(14, 3), nullable=True))


def downgrade() -> None:
    op.drop_column("sales_orders", "glacier_qty_available_for_sale")
    op.drop_column("sales_orders", "mets_qty_available_for_sale")
