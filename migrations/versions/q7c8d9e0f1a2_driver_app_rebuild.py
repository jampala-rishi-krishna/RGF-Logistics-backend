"""Driver app rebuild (pre-approved by Rishi 2026-10-13). ADDITIVE ONLY: nothing is dropped, no data is rewritten.

- form_template_items.app_only         (bool)  fields the app asks for that are not in the Zoho form
- invoice_handover_items.invoice_no    (text)  the Invoice / DR number typed at check-out
- driver_issues                        issues reported by drivers / helpers (visible to dispatch)
- driver_alert_reads                   read markers of the derived driver alerts
- seeds the real "Delivery time log" template (next version of kind delivery_checkout), mirroring the Zoho form
  "B2B Logistics Delivery" (Name, Sales Order Number, Invoice or DR Number, Time Logs). The previous 14-field DRAFT checkout
  template is only flagged inactive: its versions and every submission made with it stay readable.
"""
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "q7c8d9e0f1a2"
down_revision = "p6b7c8d9e0f1"
branch_labels = None
depends_on = None

KIND = "delivery_checkout"
NAME = "Delivery time log"
SOURCE_NOTE = (
    "Mirrors the Zoho form 'B2B Logistics Delivery': Name (the logged-in crew member), Sales Order Number, Invoice or DR Number (typed here), "
    "Time Logs = 'Arrived at Site' (check-in) / 'Departed from Site' (this form), plus the phone's GPS. Fields marked app_only are NOT in the Zoho form."
)
OUTCOMES = ["Delivered", "Partially delivered", "Failed"]
NEEDS_REASON = {"key": "outcome", "in": ["Partially delivered", "Failed"]}
# (key, label, type, required, options, show_when, app_only)
ITEMS = [
    ("invoice_dr_number", "Invoice or DR Number", "numeric_text", True, None, None, False),
    ("outcome", "Delivery outcome", "radio", True, OUTCOMES, None, True),
    ("reason", "Reason (what was not delivered, or why it failed)", "textarea", True, None, NEEDS_REASON, True),
    ("delivery_photo", "Photo (optional)", "photo", False, None, None, True),
    ("notes", "Notes (optional)", "textarea", False, None, None, True),
]


def _q(text):
    return "'" + str(text).replace("'", "''") + "'"


def _j(value):
    return "NULL" if value is None else _q(json.dumps(value)) + "::jsonb"


def _stamps():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    op.add_column("form_template_items", sa.Column("app_only", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("invoice_handover_items", sa.Column("invoice_no", sa.Text(), nullable=True))
    op.create_table(
        "driver_issues",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("reference", sa.Text(), unique=True),
        sa.Column("reporter_user_id", sa.BigInteger(), nullable=False), sa.Column("reporter_staff_id", sa.Integer()), sa.Column("reporter_role", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text()), sa.Column("so_id", sa.Text()), sa.Column("so_number", sa.Text()), sa.Column("vehicle", sa.Text()),
        sa.Column("category", sa.Text(), nullable=False), sa.Column("priority", sa.Text(), nullable=False, server_default="normal"), sa.Column("description", sa.Text(), nullable=False),
        sa.Column("photo_ids", postgresql.JSONB()), sa.Column("status", sa.Text(), nullable=False, server_default="submitted"), sa.Column("response", sa.Text()),
        sa.Column("responded_by_user_id", sa.BigInteger()), sa.Column("responded_at", sa.DateTime(timezone=True)), sa.Column("client_ts", sa.DateTime(timezone=True)),
        sa.Column("idempotency_key", sa.Text(), unique=True), *_stamps(),
    )
    op.create_index("ix_driver_issues_reporter_user_id", "driver_issues", ["reporter_user_id"])
    op.create_index("ix_driver_issues_reporter_staff_id", "driver_issues", ["reporter_staff_id"])
    op.create_index("ix_driver_issues_batch_id", "driver_issues", ["batch_id"])
    op.create_index("ix_driver_issues_status", "driver_issues", ["status"])
    op.create_table(
        "driver_alert_reads",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False), sa.Column("alert_key", sa.Text(), nullable=False), *_stamps(),
        sa.UniqueConstraint("user_id", "alert_key", name="uq_driver_alert_reads_user_key"),
    )
    op.create_index("ix_driver_alert_reads_user_id", "driver_alert_reads", ["user_id"])

    # The real time-log template becomes the next version of the checkout kind; older versions are only deactivated.
    op.execute(
        "INSERT INTO form_templates (kind, version, name, is_draft, active, source_note) "
        f"SELECT {_q(KIND)}, COALESCE(MAX(version), 0) + 1, {_q(NAME)}, false, true, {_q(SOURCE_NOTE)} FROM form_templates WHERE kind = {_q(KIND)}"
    )
    op.execute(f"UPDATE form_templates SET active = false WHERE kind = {_q(KIND)} AND name <> {_q(NAME)}")
    values = []
    for position, (key, label, item_type, required, options, show_when, app_only) in enumerate(ITEMS, 1):
        values.append(
            f"({position}, {_q(key)}, {_q(label)}, {_q(item_type)}, {'true' if required else 'false'}, {_j(options)}, NULL, {_j(show_when)}, true, {'true' if app_only else 'false'})"
        )
    op.execute(
        "INSERT INTO form_template_items (template_id, position, key, label, item_type, required, options, prefill, show_when, active, app_only) "
        "SELECT t.id, v.position, v.key, v.label, v.item_type, v.required, v.options, v.prefill, v.show_when, v.active, v.app_only FROM "
        f"(SELECT id FROM form_templates WHERE kind = {_q(KIND)} AND name = {_q(NAME)} ORDER BY version DESC LIMIT 1) t, "
        "(VALUES " + ", ".join(values) + ") AS v(position, key, label, item_type, required, options, prefill, show_when, active, app_only)"
    )


def downgrade() -> None:
    # Removes only what this migration added (the time-log template is kept if any stop was filled with it) and reactivates the newest older version.
    unused = f"kind = {_q(KIND)} AND name = {_q(NAME)} AND NOT EXISTS (SELECT 1 FROM trip_stops s WHERE s.form_template_id = form_templates.id)"
    op.execute(f"DELETE FROM form_template_items WHERE template_id IN (SELECT id FROM form_templates WHERE {unused})")
    op.execute(f"DELETE FROM form_templates WHERE {unused}")
    op.execute(
        f"UPDATE form_templates SET active = true WHERE id = (SELECT id FROM form_templates WHERE kind = {_q(KIND)} ORDER BY version DESC LIMIT 1) "
        f"AND NOT EXISTS (SELECT 1 FROM form_templates WHERE kind = {_q(KIND)} AND active)"
    )
    op.drop_table("driver_alert_reads")
    op.drop_table("driver_issues")
    op.drop_column("invoice_handover_items", "invoice_no")
    op.drop_column("form_template_items", "app_only")
