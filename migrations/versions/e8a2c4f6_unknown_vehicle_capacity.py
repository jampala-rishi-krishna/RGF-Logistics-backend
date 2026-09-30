"""Add explicit unknown-capacity placeholders for tracked vehicles."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column
from datetime import datetime, timezone

revision = "e8a2c4f6unknown"
down_revision = "d7f4a8c9e1b2"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("vehicle_capacity_profiles", sa.Column("capacity_note", sa.Text(), nullable=True))
    profiles = table("vehicle_capacity_profiles", column("created_at", sa.DateTime(timezone=True)), column("updated_at", sa.DateTime(timezone=True)), column("plate_no", sa.String), column("vehicle_type", sa.Text), column("rated_capacity_kg", sa.Float), column("is_reefer", sa.Boolean), column("is_gps_tracked", sa.Boolean), column("is_third_party", sa.Boolean))
    now = datetime.now(timezone.utc)
    op.bulk_insert(profiles, [
        {"created_at": now, "updated_at": now, "plate_no": "NAJ6018", "vehicle_type": "Tracked vehicle — capacity pending", "rated_capacity_kg": None, "is_reefer": None, "is_gps_tracked": True, "is_third_party": False},
        {"created_at": now, "updated_at": now, "plate_no": "NAN9911", "vehicle_type": "Tracked vehicle — capacity pending", "rated_capacity_kg": None, "is_reefer": None, "is_gps_tracked": True, "is_third_party": False},
    ])
    op.execute("UPDATE vehicle_capacity_profiles SET capacity_note='Capacity not yet confirmed; assignment requires dispatcher verification.' WHERE plate_no IN ('NAJ6018','NAN9911')")

def downgrade() -> None:
    op.execute("DELETE FROM vehicle_capacity_profiles WHERE plate_no IN ('NAJ6018','NAN9911')")
    op.drop_column("vehicle_capacity_profiles", "capacity_note")
