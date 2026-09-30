"""dispatch pipeline assignment, manifest, and warehouse checklist data"""
from alembic import op
import sqlalchemy as sa

revision = "d7f4a8c9e1b2"
down_revision = "bf6b497b844e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, typ in [
        ("vehicle_id", sa.String(200)), ("driver_id", sa.Integer()), ("route_id", sa.String(200)),
        ("manifest_id", sa.Integer()), ("assigned_at", sa.DateTime(timezone=True)),
        ("assigned_by", sa.Integer()), ("assignment_status", sa.String(24)),
    ]:
        op.add_column("sales_orders_cache", sa.Column(name, typ, nullable=False if name == "assignment_status" else True, server_default="unassigned" if name == "assignment_status" else None))
    op.create_index("ix_sales_orders_cache_assignment_status", "sales_orders_cache", ["assignment_status"])
    op.create_index("ix_sales_orders_cache_vehicle_id", "sales_orders_cache", ["vehicle_id"])
    op.create_table("vehicle_capacity_profiles",
        sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("vehicle_type", sa.Text(), nullable=False), sa.Column("plate_no", sa.String(200), unique=True), sa.Column("rated_capacity_kg", sa.Float()), sa.Column("is_reefer", sa.Boolean()), sa.Column("is_gps_tracked", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("is_third_party", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("driver_id", sa.Integer()))
    op.create_table("client_delivery_constraints",
        sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("customer_name", sa.Text(), nullable=False, unique=True), sa.Column("opening_time", sa.String(16)), sa.Column("receiving_cutoff_time", sa.String(16)), sa.Column("avg_processing_time_minutes", sa.Float()), sa.Column("requires_reefer", sa.Boolean()), sa.Column("notes", sa.Text()))
    op.create_table("warehouse_loading_checklists",
        sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("manifest_id", sa.Integer(), nullable=False, unique=True), sa.Column("seal_number", sa.String(200)), sa.Column("cargo_count_verified", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("cargo_count_expected", sa.Integer()), sa.Column("cargo_count_actual", sa.Integer()), sa.Column("departure_temp_c", sa.Float()), sa.Column("departure_temp_zone_count", sa.Integer()), sa.Column("driver_acknowledged", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("checklist_completed", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("completed_at", sa.DateTime(timezone=True)), sa.Column("completed_by", sa.Integer()))
    for name, typ in [("manifest_number", sa.String(80)), ("driver_id", sa.Integer()), ("origin", sa.Text()), ("destination", sa.Text()), ("total_weight_kg", sa.Float()), ("confirmed_at", sa.DateTime(timezone=True)), ("confirmed_by", sa.Integer())]:
        op.add_column("load_manifests", sa.Column(name, typ))
    for name, typ in [("salesorder_id", sa.String(200)), ("item_description", sa.Text()), ("sku", sa.String(200)), ("unit", sa.String(80)), ("weight_kg", sa.Float()), ("cold_chain_category", sa.String(32))]:
        op.add_column("manifest_items", sa.Column(name, typ))


def downgrade() -> None:
    raise NotImplementedError("Dispatch pipeline migration is intentionally not destructive.")
