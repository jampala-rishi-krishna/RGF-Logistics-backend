"""app_settings: admin-editable NON-SECRET settings (approved by Rishi 2026-10-12).

One small key/value table. Read once at startup into memory, written only when an admin saves a setting
(Admin > Settings). Environment variables stay as the fallback default for every key. Secrets (R2, Zoho, Gmail,
JWT, Cartrack, database) are never stored here: services/app_settings.py only accepts its fixed whitelist of keys.
"""
from alembic import op
import sqlalchemy as sa

revision = "p6b7c8d9e0f1"
down_revision = "o5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
