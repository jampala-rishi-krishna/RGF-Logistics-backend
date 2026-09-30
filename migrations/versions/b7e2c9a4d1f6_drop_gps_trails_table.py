"""Drop gps_trails - the last remaining GPS/position-history table in Postgres.
vehicles' live-telemetry columns were already dropped in a3d5f8c1b2e4; live GPS now
lives only in the in-memory services.live_gps_store, pushed to the frontend over
the existing WebSocket. Nothing about vehicle position is persisted anywhere."""
from alembic import op
import sqlalchemy as sa

revision = "b7e2c9a4d1f6"
down_revision = "a3d5f8c1b2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_gps_trails_vehicle_id", table_name="gps_trails")
    op.drop_table("gps_trails")


def downgrade() -> None:
    op.create_table(
        "gps_trails",
        sa.Column("vehicle_id", sa.String(length=200), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("speed_kph", sa.Float(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_gps_trails_vehicle_id", "gps_trails", ["vehicle_id"], unique=False)
