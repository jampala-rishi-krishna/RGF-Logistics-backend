"""Phase 2: sales_order_history - written only at assign/status-change/delivery, never a
bulk Zoho mirror like sales_orders_cache. Column shape mirrors sales_orders_cache so
routers/load_planning.py's _summary()/_filtered_rows() work unchanged against either table -
see models/sales_order_history.py's docstring for why raw_json is still kept for now."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e5a2f9c3d7b4"
down_revision = "d4a1c8f6e9b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sales_order_history",
        sa.Column("id", sa.String(200), nullable=False),
        sa.Column("salesorder_number", sa.Text(), nullable=True),
        sa.Column("reference_number", sa.Text(), nullable=True),
        sa.Column("customer_name", sa.Text(), nullable=True),
        sa.Column("order_status", sa.Text(), nullable=True),
        sa.Column("invoice_status", sa.Text(), nullable=True),
        sa.Column("payment_status", sa.Text(), nullable=True),
        sa.Column("shipment_status", sa.Text(), nullable=True),
        sa.Column("order_date", sa.Date(), nullable=True),
        sa.Column("expected_shipment_date", sa.Date(), nullable=True),
        sa.Column("total", sa.Numeric(14, 2), nullable=True),
        sa.Column("delivery_method", sa.Text(), nullable=True),
        sa.Column("salesperson_name", sa.Text(), nullable=True),
        sa.Column("customer_po_number", sa.Text(), nullable=True),
        sa.Column("billing_address", postgresql.JSONB(), nullable=True),
        sa.Column("shipping_address", postgresql.JSONB(), nullable=True),
        sa.Column("payment_terms_label", sa.Text(), nullable=True),
        sa.Column("mode_of_transport", sa.Text(), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("vehicle_id", sa.String(200), nullable=True),
        sa.Column("driver_id", sa.Integer(), nullable=True),
        sa.Column("route_id", sa.String(200), nullable=True),
        sa.Column("manifest_id", sa.Integer(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_by", sa.Integer(), nullable=True),
        sa.Column("assignment_status", sa.String(24), nullable=False, server_default="assigned"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sales_order_history_expected_shipment_date", "sales_order_history", ["expected_shipment_date"])
    op.create_index("ix_sales_order_history_vehicle_id", "sales_order_history", ["vehicle_id"])
    op.create_index("ix_sales_order_history_driver_id", "sales_order_history", ["driver_id"])
    op.create_index("ix_sales_order_history_route_id", "sales_order_history", ["route_id"])
    op.create_index("ix_sales_order_history_manifest_id", "sales_order_history", ["manifest_id"])
    op.create_index("ix_sales_order_history_assignment_status", "sales_order_history", ["assignment_status"])
    op.create_index("ix_sales_order_history_completed_at", "sales_order_history", ["completed_at"])


def downgrade() -> None:
    op.drop_table("sales_order_history")
