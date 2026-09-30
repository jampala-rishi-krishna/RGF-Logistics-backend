"""Drop tables confirmed to have zero read/write sites anywhere in the live app
(audited 2026-09-24): sessions, tenant_scopes, auth_events (JWT auth is stateless, and
login/logout no longer write an audit row); message_events, voice_calls, webhook_events,
communication_preferences, contact_methods, communication_jobs (routers/comms.py's retired
simulated-messaging module - the frontend never called any of these, confirmed by grepping
every commsApi.* call site); staff_directory (n8n's Logistics Staff Directory DataTable is
now the only source, see services/staff_directory_cache.py).

NOT dropped: conversations, messages (routers/agent.py's AI chat assistant still uses these -
comms.py just stopped touching them)."""
from alembic import op
import sqlalchemy as sa

revision = "d4a1c8f6e9b2"
down_revision = "c9f3a7e2b5d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("staff_directory")
    op.drop_table("communication_jobs")
    op.drop_table("contact_methods")
    op.drop_table("communication_preferences")
    op.drop_table("webhook_events")
    op.drop_table("voice_calls")
    op.drop_table("message_events")
    op.drop_table("auth_events")
    op.drop_table("tenant_scopes")
    op.drop_table("sessions")


def downgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("user_id", sa.String(200), nullable=True),
        sa.Column("login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("logout_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.String(200), nullable=True),
        sa.Column("device_info", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "tenant_scopes",
        sa.Column("user_id", sa.String(200), nullable=True),
        sa.Column("scope_type", sa.Text(), nullable=True),
        sa.Column("scope_value", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "auth_events",
        sa.Column("user_id", sa.String(200), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=True),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "message_events",
        sa.Column("message_id", sa.String(200), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=True),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_message_events_message_id", "message_events", ["message_id"])
    op.create_table(
        "voice_calls",
        sa.Column("contact_id", sa.String(200), nullable=True),
        sa.Column("call_state", sa.Text(), nullable=True),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column("transfer_result", sa.Text(), nullable=True),
        sa.Column("consent_flag", sa.Boolean(), nullable=True),
        sa.Column("recording_ref", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_voice_calls_contact_id", "voice_calls", ["contact_id"])
    op.create_table(
        "webhook_events",
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("payload_ref", sa.Text(), nullable=True),
        sa.Column("signature_status", sa.Text(), nullable=True),
        sa.Column("processing_result", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "communication_preferences",
        sa.Column("user_id", sa.String(200), nullable=True),
        sa.Column("consent", sa.Boolean(), nullable=True),
        sa.Column("opt_out", sa.Boolean(), nullable=True),
        sa.Column("quiet_hours_start", sa.Text(), nullable=True),
        sa.Column("quiet_hours_end", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("fallback_rules_json", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_communication_preferences_user_id", "communication_preferences", ["user_id"])
    op.create_table(
        "contact_methods",
        sa.Column("user_id", sa.String(200), nullable=True),
        sa.Column("method_type", sa.Text(), nullable=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("verification_state", sa.Text(), nullable=True),
        sa.Column("is_preferred", sa.Boolean(), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contact_methods_user_id", "contact_methods", ["user_id"])
    op.create_table(
        "communication_jobs",
        sa.Column("message_id", sa.String(200), nullable=True),
        sa.Column("queue_state", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=True),
        sa.Column("scheduled_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_communication_jobs_message_id", "communication_jobs", ["message_id"])
    op.create_table(
        "staff_directory",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("warehouse", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
