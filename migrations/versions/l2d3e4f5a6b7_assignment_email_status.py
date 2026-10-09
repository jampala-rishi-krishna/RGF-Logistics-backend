"""sales_orders: assignment email status columns (approved by Rishi 2026-10-09).

One email covers several SOs, so all rows of an assignment batch are updated together. Written only on a
state change (queued -> sent / failed / skipped); nothing polls or rewrites them.
"""
from alembic import op
import sqlalchemy as sa

revision = "l2d3e4f5a6b7"
down_revision = "k1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sales_orders", sa.Column("email_status", sa.Text(), nullable=True))
    op.add_column("sales_orders", sa.Column("email_error", sa.Text(), nullable=True))
    op.add_column("sales_orders", sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sales_orders", sa.Column("email_message_id", sa.Text(), nullable=True))


def downgrade() -> None:
    for column in ("email_message_id", "email_sent_at", "email_error", "email_status"):
        op.drop_column("sales_orders", column)
