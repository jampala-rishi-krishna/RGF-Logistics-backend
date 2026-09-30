"""Drop live-telemetry columns from vehicles - GPS now lives only in the in-memory
services.live_gps_store, never in Postgres. vehicles keeps static roster data only
(plate_no, driver_id)."""
from alembic import op
import sqlalchemy as sa

revision = "a3d5f8c1b2e4"
down_revision = "f1a9b3c7d8e0"
branch_labels = None
depends_on = None

_DROPPED_COLUMNS = [
    ("status", sa.Text()),
    ("fuel_pct", sa.Numeric(5, 2)),
    ("ignition_on", sa.Boolean()),
    ("current_lat", sa.Float()),
    ("current_lng", sa.Float()),
    ("heading", sa.Float()),
    ("speed_kph", sa.Float()),
    ("zone", sa.Text()),
    ("last_updated", sa.DateTime(timezone=True)),
]


def upgrade() -> None:
    for name, _ in _DROPPED_COLUMNS:
        op.drop_column("vehicles", name)


def downgrade() -> None:
    for name, coltype in reversed(_DROPPED_COLUMNS):
        op.add_column("vehicles", sa.Column(name, coltype, nullable=True))
