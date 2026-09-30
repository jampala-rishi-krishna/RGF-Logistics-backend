"""Store assignment helpers and seed the six known third-party trucks."""
from alembic import op
import sqlalchemy as sa

revision = "i1a2b3c4d5e6"
down_revision = "h9e0f1a2b3c4"
branch_labels = None
depends_on = None

THIRD_PARTY = ["ASIAN CONNECT", "ASIAN CONNECT 2", "ASIAN CONNECT 3", "INHOUSE RIDER", "WETMARKET", "BHENTZ"]

def upgrade() -> None:
    op.add_column("sales_orders", sa.Column("helper_ids", sa.JSON(), nullable=True))
    bind = op.get_bind()
    for index, plate in enumerate(THIRD_PARTY, 1):
        op.execute(sa.text(f"""
            INSERT INTO vehicles
                (id, created_at, updated_at, plate_no, driver_id, vehicle_type,
                 is_gps_tracked, is_third_party)
            SELECT {-91000 - index}, now(), now(), '{plate}', NULL, 'Third-party truck', false, true
            WHERE NOT EXISTS (
                SELECT 1 FROM vehicles WHERE upper(plate_no) = upper('{plate}')
            )
        """))
        op.execute(sa.text(f"""
            UPDATE vehicles SET is_third_party = true, is_gps_tracked = false, updated_at = now()
            WHERE upper(plate_no) = upper('{plate}')
        """))

def downgrade() -> None:
    bind = op.get_bind()
    for plate in THIRD_PARTY:
        op.execute(sa.text(f"""
            DELETE FROM vehicles v
            WHERE upper(v.plate_no) = upper('{plate}')
              AND v.is_third_party = true
              AND NOT EXISTS (SELECT 1 FROM sales_orders s WHERE s.vehicle_id = v.plate_no)
        """))
    op.drop_column("sales_orders", "helper_ids")
