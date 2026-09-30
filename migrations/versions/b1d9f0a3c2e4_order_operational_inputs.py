"""add explicit order weight and service time inputs

Revision ID: b1d9f0a3c2e4
Revises: 0a66f447f5fc
"""

from alembic import op
import sqlalchemy as sa


revision = "b1d9f0a3c2e4"
down_revision = "0a66f447f5fc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("shipment_weight", sa.Numeric(12, 3), nullable=True))
    op.add_column("orders", sa.Column("shipment_weight_unit", sa.String(length=16), nullable=True))
    op.add_column("orders", sa.Column("service_time_min", sa.Numeric(8, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "service_time_min")
    op.drop_column("orders", "shipment_weight_unit")
    op.drop_column("orders", "shipment_weight")
