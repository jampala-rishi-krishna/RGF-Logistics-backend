"""Drop the retired in-memory AI message persistence table."""
from alembic import op

revision = "h9e0f1a2b3c4"
down_revision = "g8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("messages", if_exists=True)


def downgrade() -> None:
    from database import TimestampedBase
    import models  # noqa: F401
    table = TimestampedBase.metadata.tables.get("messages")
    if table is not None:
        table.create(op.get_bind(), checkfirst=True)
