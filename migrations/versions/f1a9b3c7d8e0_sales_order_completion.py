"""Track soft-completed sales-order assignments."""
from alembic import op
import sqlalchemy as sa

revision = "f1a9b3c7d8e0"
down_revision = "e8a2c4f6unknown"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("sales_orders_cache", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_sales_orders_cache_completed_at", "sales_orders_cache", ["completed_at"])

def downgrade() -> None:
    op.drop_index("ix_sales_orders_cache_completed_at", table_name="sales_orders_cache")
    op.drop_column("sales_orders_cache", "completed_at")
