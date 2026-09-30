"""Merge vehicle profiles and remove retired persistence tables.

The application now keeps live Zoho/dispatch state in memory and only persists
the approved history, manifest, constraint, checklist, and user/vehicle tables.
"""
from alembic import op
import sqlalchemy as sa

revision = "g8d9e0f1a2b3"
down_revision = "f7c8d9e0a1b2"
branch_labels = None
depends_on = None

_DROPPED = [
    "alert_escalations", "alerts", "audit_log", "conversations", "customer_contacts",
    "customers", "delivery_proofs", "drivers", "geofences", "integration_status",
    "message_log", "message_templates", "notification_templates", "optimization_run_routes",
    "optimization_run_stops", "optimization_runs", "order_events", "orders", "roles",
    "route_stops", "routes", "sales_orders_cache", "vehicle_capacity_profiles",
    "vehicle_operating_profiles", "warehouse", "warehouse_events",
]


def upgrade() -> None:
    columns = [
        ("vehicle_type", sa.Text()),
        ("rated_capacity_kg", sa.Float()),
        ("capacity_note", sa.Text()),
        ("is_reefer", sa.Boolean()),
        ("is_gps_tracked", sa.Boolean(), {"server_default": sa.true(), "nullable": False}),
        ("is_third_party", sa.Boolean(), {"server_default": sa.false(), "nullable": False}),
        ("capacity_kg", sa.Numeric(10, 2)),
        ("capacity_m3", sa.Numeric(10, 2)),
        ("temperature_capability", sa.Text()),
        ("shift_start", sa.Text()),
        ("shift_end", sa.Text()),
        ("cost_per_km", sa.Numeric(10, 2)),
        ("cost_per_hour", sa.Numeric(10, 2)),
        ("depot_lat", sa.Numeric(9, 6)),
        ("depot_lng", sa.Numeric(9, 6)),
    ]
    for entry in columns:
        name, typ, *opts = entry
        op.add_column("vehicles", sa.Column(name, typ, **(opts[0] if opts else {})))

    op.execute("""
        UPDATE vehicles v
        SET vehicle_type = p.vehicle_type,
            rated_capacity_kg = p.rated_capacity_kg,
            capacity_note = p.capacity_note,
            is_reefer = p.is_reefer,
            is_gps_tracked = p.is_gps_tracked,
            is_third_party = p.is_third_party,
            driver_id = COALESCE(v.driver_id, p.driver_id::text)
        FROM vehicle_capacity_profiles p
        WHERE upper(v.plate_no) = upper(p.plate_no)
    """)
    op.execute("""
        UPDATE vehicles v
        SET capacity_kg = p.capacity_kg,
            capacity_m3 = p.capacity_m3,
            temperature_capability = p.temperature_capability,
            shift_start = p.shift_start,
            shift_end = p.shift_end,
            cost_per_km = p.cost_per_km,
            cost_per_hour = p.cost_per_hour,
            depot_lat = p.depot_lat,
            depot_lng = p.depot_lng
        FROM vehicle_operating_profiles p
        WHERE v.id = p.vehicle_id
    """)
    for table in _DROPPED:
        op.drop_table(table, if_exists=True)


def downgrade() -> None:
    # Recreate the retired model tables from the checked-in SQLAlchemy metadata,
    # then restore the two profile tables' data from the merged vehicle columns.
    from database import TimestampedBase
    import models  # noqa: F401 - imports all mapped tables into metadata

    metadata = TimestampedBase.metadata
    for table_name in _DROPPED:
        table = metadata.tables.get(table_name)
        if table is not None:
            table.create(op.get_bind(), checkfirst=True)

    op.execute("""
        INSERT INTO vehicle_capacity_profiles
            (id, created_at, updated_at, vehicle_type, plate_no, rated_capacity_kg,
             capacity_note, is_reefer, is_gps_tracked, is_third_party, driver_id)
        SELECT id, created_at, updated_at, vehicle_type, plate_no, rated_capacity_kg,
               capacity_note, is_reefer, is_gps_tracked, is_third_party,
               NULLIF(driver_id, '')::integer
        FROM vehicles
        ON CONFLICT (id) DO NOTHING
    """)
    for name in [
        "depot_lng", "depot_lat", "cost_per_hour", "cost_per_km", "shift_end",
        "shift_start", "temperature_capability", "capacity_m3", "capacity_kg",
        "is_third_party", "is_gps_tracked", "is_reefer", "capacity_note",
        "rated_capacity_kg", "vehicle_type",
    ]:
        op.drop_column("vehicles", name)
