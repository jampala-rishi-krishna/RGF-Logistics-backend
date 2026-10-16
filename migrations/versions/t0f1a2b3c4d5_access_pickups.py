"""Approved role, manager, assignment and pickup workflows. No live-order mirror."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "t0f1a2b3c4d5"
down_revision = "s9e0f1a2b3c4"
branch_labels = depends_on = None


def upgrade():
    op.alter_column("users", "phone", existing_type=sa.BigInteger(), nullable=True)
    # PostgreSQL UNIQUE already permits multiple NULLs and still rejects duplicate numbers.
    op.add_column("users", sa.Column("is_manager", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("sales_orders", sa.Column("assigned_by_name", sa.Text(), nullable=True))
    connection = op.get_bind()
    changed = connection.execute(sa.text("UPDATE users SET role='planner' WHERE role='dispatcher' RETURNING email")).scalars().all()
    print("Dispatcher -> Planner:", list(changed))
    connection.execute(sa.text("UPDATE users SET is_manager=true WHERE role='admin' OR lower(email) IN ('pau@rareglobalfood.com','jed@rareglobalfood.com')"))
    common = lambda: [sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
                        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
                        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())]
    op.create_table("unassign_requests", *common(), sa.Column("order_id", sa.Text(), nullable=False), sa.Column("order_number", sa.Text(), nullable=False),
        sa.Column("requester_id", sa.Integer(), nullable=False), sa.Column("requester_name", sa.Text(), nullable=False), sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False), sa.Column("resolved_by", sa.Integer()), sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text()), sa.Column("manager_reads", postgresql.JSONB(), nullable=False), sa.Column("result_read", sa.Boolean(), nullable=False))
    op.create_index("uq_unassign_pending_order", "unassign_requests", ["order_id"], unique=True, postgresql_where=sa.text("status='pending'"))
    op.create_table("pickup_decisions", *common(), sa.Column("decision_key", sa.Text(), nullable=False, unique=True), sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("delivery_date", sa.Date(), nullable=False), sa.Column("status", sa.Text(), nullable=False), sa.Column("decided_by", sa.Integer(), nullable=False),
        sa.Column("decided_by_name", sa.Text(), nullable=False))
    op.create_table("pickup_email_logs", *common(), sa.Column("delivery_date", sa.Date(), nullable=False), sa.Column("recipients", postgresql.JSONB(), nullable=False),
        sa.Column("po_count", sa.Integer(), nullable=False), sa.Column("status", sa.Text(), nullable=False), sa.Column("error", sa.Text()), sa.Column("schedule_key", sa.Text(), unique=True))


def downgrade():
    raise RuntimeError("Forward-only: rollback would delete decisions and send history. Restore application code without deleting data.")
