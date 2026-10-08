"""Fleet Health: daily vehicle stats, service intervals, maintenance records, flags, pre-trip checklists, fuel logs.

Six tables, all tiny (see expected rows/year). No raw trips/events are stored.
Enumerations are VARCHAR + CHECK (not native PG ENUM) so a new value later is a one-line CHECK change,
and downgrade is clean. Staff live in n8n, not Neon, so staff ids are plain integers (no FK).
"""
from alembic import op
import sqlalchemy as sa

revision = "j1b2c3d4e5f6"
down_revision = "i1a2b3c4d5e6"
branch_labels = None
depends_on = None

SERVICE_TYPES = ("oil_change", "tires", "brakes", "reefer_service", "general_pms", "battery")
FLAG_SOURCES = ("voice", "email", "whatsapp", "checklist", "battery", "fuel", "overload", "manual", "driver_app")
SEVERITIES = ("info", "warning", "critical")
KINDS = ("service", "repair", "inspection")


def _in(column: str, values) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _base():
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _vehicle_fk(nullable=False):
    return sa.Column("vehicle_id", sa.BigInteger(), sa.ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=nullable)


def upgrade() -> None:
    # 1. one row per tracked vehicle per day (~6 x 365 = 2,190 rows/year)
    op.create_table(
        "vehicle_daily_stats", *_base(), _vehicle_fk(),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("odometer_start_km", sa.Numeric(10, 2)), sa.Column("odometer_end_km", sa.Numeric(10, 2)), sa.Column("km_driven", sa.Numeric(10, 2)),
        sa.Column("trip_count", sa.Integer()), sa.Column("engine_seconds", sa.Integer()),
        sa.Column("idle_seconds_total", sa.Integer()), sa.Column("idle_seconds_at_stop", sa.Integer()), sa.Column("idle_seconds_elsewhere", sa.Integer()),
        sa.Column("speeding_events", sa.Integer()), sa.Column("speeding_seconds", sa.Integer()), sa.Column("max_speed_kmh", sa.Integer()),
        sa.Column("harsh_braking", sa.Integer()), sa.Column("harsh_acceleration", sa.Integer()), sa.Column("harsh_cornering", sa.Integer()),
        sa.Column("vext_parked_min", sa.Numeric(5, 2)), sa.Column("vext_running_avg", sa.Numeric(5, 2)), sa.Column("electrical_system", sa.SmallInteger()),
        sa.Column("fuel_pct_start", sa.Numeric(5, 2)), sa.Column("fuel_pct_end", sa.Numeric(5, 2)),
        sa.Column("refuel_events", sa.Integer()), sa.Column("refuel_litres_est", sa.Numeric(7, 2)), sa.Column("parked_drop_litres_est", sa.Numeric(7, 2)),
        sa.Column("primary_staff_id", sa.Integer()),  # n8n staff-directory id (no FK: staff are not in Neon)
        sa.Column("assigned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("data_quality", sa.JSON()),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("vehicle_id", "stat_date", name="uq_vehicle_daily_stats_vehicle_date"),
        sa.CheckConstraint("electrical_system IS NULL OR electrical_system IN (12, 24)", name="ck_vds_electrical_system"),
    )
    op.create_index("ix_vehicle_daily_stats_stat_date", "vehicle_daily_stats", ["stat_date"])

    # 2. service intervals: fleet defaults (vehicle_id NULL) + per-truck overrides (< 50 rows)
    op.create_table(
        "service_intervals", *_base(), _vehicle_fk(nullable=True),
        sa.Column("service_type", sa.String(24), nullable=False),
        sa.Column("interval_km", sa.Integer()), sa.Column("interval_engine_hours", sa.Integer()), sa.Column("interval_days", sa.Integer()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),  # false = placeholder, "confirm with logistics"
        sa.Column("updated_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.CheckConstraint(_in("service_type", SERVICE_TYPES), name="ck_service_intervals_type"),
        sa.CheckConstraint("interval_km IS NOT NULL OR interval_engine_hours IS NOT NULL OR interval_days IS NOT NULL", name="ck_service_intervals_has_interval"),
    )
    op.create_index("ux_service_intervals_default", "service_intervals", ["service_type"], unique=True, postgresql_where=sa.text("vehicle_id IS NULL"))
    op.create_index("ux_service_intervals_truck", "service_intervals", ["vehicle_id", "service_type"], unique=True, postgresql_where=sa.text("vehicle_id IS NOT NULL"))

    # 3. maintenance records: services, repairs, inspections, downtime (~a few hundred/year)
    op.create_table(
        "maintenance_records", *_base(), _vehicle_fk(),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("service_type", sa.String(24)),
        sa.Column("performed_on", sa.Date(), nullable=False),
        sa.Column("odometer_km", sa.Numeric(10, 2)), sa.Column("engine_hours", sa.Numeric(10, 2)),
        sa.Column("downtime_start", sa.DateTime(timezone=True)), sa.Column("downtime_end", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.Text()), sa.Column("cost_php", sa.Numeric(12, 2)), sa.Column("vendor", sa.Text()),
        sa.Column("notes", sa.Text()), sa.Column("receipt_ref", sa.Text()),  # reference/link only, no files in Neon
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.CheckConstraint(_in("kind", KINDS), name="ck_maintenance_kind"),
        sa.CheckConstraint("service_type IS NULL OR " + _in("service_type", SERVICE_TYPES), name="ck_maintenance_service_type"),
    )
    op.create_index("ix_maintenance_records_vehicle_performed", "maintenance_records", ["vehicle_id", "performed_on"])

    # 4. open issues per truck, resolvable. ONE entry point writes here: services/vehicle_flags.py report_issue()
    op.create_table(
        "vehicle_flags", *_base(), _vehicle_fk(),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text()),
        sa.Column("photo_ref", sa.Text()),            # link/reference only (future driver app)
        sa.Column("reported_by", sa.Text()),          # free text identity of the reporter (user / staff id / driver app)
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
        sa.Column("dedup_key", sa.String(40), nullable=False),  # sha1(vehicle|source|manila date|message)
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("resolution_note", sa.Text()),
        sa.CheckConstraint(_in("source", FLAG_SOURCES), name="ck_vehicle_flags_source"),
        sa.CheckConstraint(_in("severity", SEVERITIES), name="ck_vehicle_flags_severity"),
    )
    op.create_index("ix_vehicle_flags_open", "vehicle_flags", ["vehicle_id"], postgresql_where=sa.text("resolved_at IS NULL"))
    # Dedup only among OPEN flags: a resolved flag can be raised again with the same text.
    op.create_index("ux_vehicle_flags_open_dedup", "vehicle_flags", ["dedup_key"], unique=True, postgresql_where=sa.text("resolved_at IS NULL"), sqlite_where=sa.text("resolved_at IS NULL"))

    # 5. pre-trip checklists (~1 per truck per working day)
    op.create_table(
        "pretrip_checklists", *_base(), _vehicle_fk(),
        sa.Column("staff_id", sa.Integer()),          # n8n staff-directory id (no FK)
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entered_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("items", sa.JSON(), nullable=False),  # {tires, lights, brakes, leaks, mirrors_wipers, body_damage, reefer_running, documents} -> ok|issue|na
        sa.Column("reefer_temp_c", sa.Numeric(5, 1)), sa.Column("notes", sa.Text()),
        sa.Column("passed", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_pretrip_checklists_vehicle_checked", "pretrip_checklists", ["vehicle_id", "checked_at"])

    # 6. fuel logs (~1 per truck per few days)
    op.create_table(
        "fuel_logs", *_base(), _vehicle_fk(),
        sa.Column("staff_id", sa.Integer()),          # n8n staff-directory id (no FK)
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("litres", sa.Numeric(7, 2), nullable=False), sa.Column("amount_php", sa.Numeric(10, 2), nullable=False),
        sa.Column("odometer_km", sa.Numeric(10, 2)), sa.Column("full_tank", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("station", sa.Text()), sa.Column("receipt_ref", sa.Text()),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.CheckConstraint("litres > 0", name="ck_fuel_logs_litres_positive"),
    )
    op.create_index("ix_fuel_logs_vehicle_filled", "fuel_logs", ["vehicle_id", "filled_at"])

    # fleet-default intervals: PLACEHOLDERS, confirmed=false until logistics edits them
    op.execute(sa.text("""
        INSERT INTO service_intervals (service_type, interval_km, interval_engine_hours, interval_days, active, confirmed) VALUES
          ('oil_change',     10000, 250, 180, true, false),
          ('tires',          40000, NULL, 730, true, false),
          ('brakes',         30000, NULL, 365, true, false),
          ('reefer_service', NULL,  500, 180, true, false),
          ('general_pms',    20000, 500, 365, true, false),
          ('battery',        NULL,  NULL, 730, true, false)
    """))


def downgrade() -> None:
    for table in ("fuel_logs", "pretrip_checklists", "vehicle_flags", "maintenance_records", "service_intervals", "vehicle_daily_stats"):
        op.drop_table(table)
