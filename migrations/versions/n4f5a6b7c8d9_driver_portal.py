"""Driver portal (approved by Rishi 2026-10-12).

- users.staff_id: links a login to its n8n staff directory record (unique, nullable).
- user_invites: single-use, expiring (24h) set-password / reset links; only the token hash is stored.
- sales_orders.delivery_notes / delivery_contact: copied from the already-cached SO detail inside the assignment
  UPDATE (no extra write, no Zoho call) so the driver app can show them after a restart and in history.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "n4f5a6b7c8d9"
down_revision = "m3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("staff_id", sa.Integer(), nullable=True))
    op.create_index("ix_users_staff_id", "users", ["staff_id"], unique=True)
    op.create_table(
        "user_invites",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("token_hash", name="uq_user_invites_token_hash"),
    )
    op.create_index("ix_user_invites_user_id", "user_invites", ["user_id"])
    op.add_column("sales_orders", sa.Column("delivery_notes", sa.Text(), nullable=True))
    op.add_column("sales_orders", sa.Column("delivery_contact", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("sales_orders", "delivery_contact")
    op.drop_column("sales_orders", "delivery_notes")
    op.drop_index("ix_user_invites_user_id", table_name="user_invites")
    op.drop_table("user_invites")
    op.drop_index("ix_users_staff_id", table_name="users")
    op.drop_column("users", "staff_id")
