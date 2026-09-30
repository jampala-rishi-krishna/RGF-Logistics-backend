"""Add users.must_change_password, so a seeded/reset admin account can be forced to
change its password on first login instead of trusting the printed seed password forever."""
from alembic import op
import sqlalchemy as sa

revision = "c9f3a7e2b5d1"
down_revision = "b7e2c9a4d1f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column("users", "must_change_password", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
