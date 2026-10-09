"""sales_orders.assignment_batch_id: which assignment a row belongs to (approved by Rishi 2026-10-09).

Written in the same single UPDATE as the first ("queued") email-status write, so it costs no extra write. Retry /
Resend resolve the SOs of an assignment from this id, so two separate assignments to the same truck can never be
merged into one email.
"""
from alembic import op
import sqlalchemy as sa

revision = "m3e4f5a6b7c8"
down_revision = "l2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sales_orders", sa.Column("assignment_batch_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sales_orders", "assignment_batch_id")
