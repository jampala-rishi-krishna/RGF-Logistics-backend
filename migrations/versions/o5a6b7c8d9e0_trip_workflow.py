"""Driver/helper trip workflow + officer invoice validation (pre-approved by Rishi 2026-10-12).

Tables: form_templates, form_template_items, trips, so_acknowledgements, trip_stops, trip_events, trip_photos,
invoice_handovers, invoice_handover_items. Seeds two DRAFT, ACTIVE templates (acknowledge checklist, delivery checkout) so
the app works immediately; Admin replaces them (creating version 2 ...) from Driver Overview > Checklist settings.
No blobs are stored in Neon (photos live in object storage; trip_photos holds keys only).
"""
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "o5a6b7c8d9e0"
down_revision = "n4f5a6b7c8d9"
branch_labels = None
depends_on = None

ID = lambda: sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True)  # noqa: E731
STAMPS = lambda: (  # noqa: E731
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
)
TS = lambda name, nullable=True: sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)  # noqa: E731
JSON = lambda name, nullable=True: sa.Column(name, postgresql.JSONB(), nullable=nullable)  # noqa: E731

ACK_ITEMS = [
    ("products_match", "Products match the SO lines and quantities", "checkbox", True),
    ("qty_verified", "Packs / kg verified against the SO", "checkbox", True),
    ("invoice_copies", "Invoice copies received", "checkbox", True),
    ("delivery_docs", "Delivery receipt / documents received", "checkbox", True),
    ("temp_ok", "Cold-chain / temperature OK", "checkbox", True),
    ("packaging_ok", "Packaging intact", "checkbox", True),
    ("packs_received", "Number of packs received", "count", False),
    ("loaded_photo", "Photo of the loaded goods", "photo", False),
    ("remarks", "Notes / discrepancies", "note", False),
]
OUTCOMES = ["Delivered", "Partially delivered", "Failed / Returned"]
# (key, label, type, required, options, prefill, show_when)
CHECKOUT_ITEMS = [
    ("so_number", "SO number", "text", True, None, "so_number", None),
    ("customer", "Customer", "text", True, None, "customer", None),
    ("delivery_address", "Delivery address", "textarea", True, None, "address", None),
    ("delivery_date", "Delivery date", "date", True, None, "date", None),
    ("driver", "Driver", "text", True, None, "driver", None),
    ("helper", "Helper", "text", False, None, "helper", None),
    ("vehicle", "Vehicle / plate no.", "text", True, None, "vehicle", None),
    ("outcome", "Delivery outcome", "dropdown", True, OUTCOMES, None, None),
    ("received_by", "Received by (name of the person at the customer)", "text", True, None, None, {"key": "outcome", "in": ["Delivered", "Partially delivered"]}),
    ("reason", "Reason (what was not delivered, or why it failed / was returned)", "textarea", True, None, None, {"key": "outcome", "in": ["Partially delivered", "Failed / Returned"]}),
    ("temperature_c", "Temperature at delivery (deg C)", "number", False, None, None, None),
    ("delivery_photo", "Photo of the delivery", "photo", False, None, None, None),
    ("signature", "Customer signature", "signature", False, None, None, None),
    ("remarks", "Remarks", "textarea", False, None, None, None),
]


def _q(text):
    return "'" + str(text).replace("'", "''") + "'"


def _b(value):
    return "true" if value else "false"


def _j(value):
    return "NULL" if value is None else _q(json.dumps(value)) + "::jsonb"


def upgrade() -> None:
    op.create_table(
        "form_templates", ID(),
        sa.Column("kind", sa.Text(), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_draft", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_note", sa.Text()), sa.Column("created_by_user_id", sa.BigInteger()), *STAMPS(),
        sa.UniqueConstraint("kind", "version", name="uq_form_templates_kind_version"),
    )
    op.create_index("ix_form_templates_kind", "form_templates", ["kind"])
    op.create_table(
        "form_template_items", ID(),
        sa.Column("template_id", sa.BigInteger(), sa.ForeignKey("form_templates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False), sa.Column("key", sa.Text(), nullable=False), sa.Column("label", sa.Text(), nullable=False),
        sa.Column("item_type", sa.Text(), nullable=False), sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
        JSON("options"), sa.Column("prefill", sa.Text()), JSON("show_when"), sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()), *STAMPS(),
        sa.UniqueConstraint("template_id", "key", name="uq_form_template_items_template_key"),
    )
    op.create_index("ix_form_template_items_template_id", "form_template_items", ["template_id"])

    op.create_table(
        "trips", ID(),
        sa.Column("batch_id", sa.Text(), nullable=False, unique=True), sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("delivery_date", sa.Date(), nullable=False), sa.Column("vehicle", sa.Text()), sa.Column("driver_staff_id", sa.Integer()),
        JSON("helper_staff_ids"), sa.Column("warehouse", sa.Text()),
        TS("started_at"), TS("started_received_at"), sa.Column("started_by_user_id", sa.BigInteger()), JSON("started_gps"),
        TS("ended_at"), TS("ended_received_at"), sa.Column("ended_by_user_id", sa.BigInteger()), JSON("ended_gps"), sa.Column("end_distance_m", sa.Float()),
        TS("closed_at"), sa.Column("stop_order_source", sa.Text()), *STAMPS(),
    )
    op.create_index("ix_trips_status", "trips", ["status"])
    op.create_index("ix_trips_delivery_date", "trips", ["delivery_date"])
    op.create_index("ix_trips_driver_staff_id", "trips", ["driver_staff_id"])

    op.create_table(
        "so_acknowledgements", ID(),
        sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE"), nullable=False), sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("so_id", sa.Text(), nullable=False), sa.Column("template_id", sa.BigInteger(), sa.ForeignKey("form_templates.id"), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False), JSON("answers", nullable=False), sa.Column("discrepancy_note", sa.Text()),
        TS("acknowledged_at", False), TS("received_at", False), sa.Column("by_user_id", sa.BigInteger(), nullable=False), sa.Column("by_role", sa.Text(), nullable=False),
        sa.Column("by_staff_id", sa.Integer()), JSON("gps"), *STAMPS(),
        sa.UniqueConstraint("trip_id", "so_id", name="uq_so_ack_trip_so"),
    )
    op.create_index("ix_so_acknowledgements_trip_id", "so_acknowledgements", ["trip_id"])
    op.create_index("ix_so_acknowledgements_batch_id", "so_acknowledgements", ["batch_id"])
    op.create_index("ix_so_acknowledgements_so_id", "so_acknowledgements", ["so_id"])

    op.create_table(
        "trip_stops", ID(),
        sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE"), nullable=False), sa.Column("so_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False), sa.Column("so_number", sa.Text()), sa.Column("customer_name", sa.Text()), sa.Column("address_text", sa.Text()),
        sa.Column("lat", sa.Float()), sa.Column("lng", sa.Float()), sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        TS("checkin_at"), TS("checkin_received_at"), sa.Column("checkin_by_user_id", sa.BigInteger()), JSON("checkin_gps"), sa.Column("checkin_distance_m", sa.Float()), sa.Column("checkin_far", sa.Boolean()),
        TS("checkout_at"), TS("checkout_received_at"), sa.Column("checkout_by_user_id", sa.BigInteger()), JSON("checkout_gps"),
        sa.Column("outcome", sa.Text()), sa.Column("reason", sa.Text()), sa.Column("form_template_id", sa.BigInteger(), sa.ForeignKey("form_templates.id")),
        sa.Column("form_template_version", sa.Integer()), JSON("form_answers"), *STAMPS(),
        sa.UniqueConstraint("trip_id", "so_id", name="uq_trip_stops_trip_so"),
    )
    op.create_index("ix_trip_stops_trip_id", "trip_stops", ["trip_id"])
    op.create_index("ix_trip_stops_so_id", "trip_stops", ["so_id"])

    op.create_table(
        "trip_events", ID(),
        sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE")), sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False), sa.Column("so_id", sa.Text()), sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False), sa.Column("staff_id", sa.Integer()), TS("client_ts", False), TS("received_at", False), JSON("gps"),
        sa.Column("idempotency_key", sa.Text(), unique=True), JSON("payload"), JSON("result"), *STAMPS(),
    )
    op.create_index("ix_trip_events_trip_id", "trip_events", ["trip_id"])
    op.create_index("ix_trip_events_batch_id", "trip_events", ["batch_id"])
    op.create_index("ix_trip_events_so_id", "trip_events", ["so_id"])
    op.create_index("ix_trip_events_user_id", "trip_events", ["user_id"])
    op.create_index("ix_trip_events_received_at", "trip_events", ["received_at"])

    op.create_table(
        "trip_photos", ID(),
        sa.Column("batch_id", sa.Text(), nullable=False), sa.Column("so_id", sa.Text()), sa.Column("field_key", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False, unique=True), sa.Column("content_type", sa.Text(), nullable=False), sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("uploaded_by_user_id", sa.BigInteger(), nullable=False), sa.Column("upload_key", sa.Text(), unique=True), *STAMPS(),
    )
    op.create_index("ix_trip_photos_batch_id", "trip_photos", ["batch_id"])

    op.create_table(
        "invoice_handovers", ID(),
        sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE"), nullable=False, unique=True), sa.Column("batch_id", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"), sa.Column("submitted_by_user_id", sa.BigInteger(), nullable=False),
        TS("submitted_at", False), TS("submitted_received_at", False), JSON("submitted_gps"), sa.Column("officer_user_id", sa.BigInteger()),
        TS("officer_acknowledged_at"), sa.Column("officer_acknowledged_by_user_id", sa.BigInteger()), sa.Column("note", sa.Text()), *STAMPS(),
    )
    op.create_index("ix_invoice_handovers_status", "invoice_handovers", ["status"])
    op.create_index("ix_invoice_handovers_officer_user_id", "invoice_handovers", ["officer_user_id"])

    op.create_table(
        "invoice_handover_items", ID(),
        sa.Column("handover_id", sa.BigInteger(), sa.ForeignKey("invoice_handovers.id", ondelete="CASCADE"), nullable=False), sa.Column("so_id", sa.Text(), nullable=False),
        sa.Column("so_number", sa.Text()), sa.Column("customer_name", sa.Text()), sa.Column("handed_over", sa.Boolean(), nullable=False), sa.Column("crew_note", sa.Text()),
        sa.Column("officer_status", sa.Text(), nullable=False, server_default="pending"), sa.Column("officer_note", sa.Text()), sa.Column("officer_user_id", sa.BigInteger()),
        TS("officer_checked_at"), TS("resolved_at"), *STAMPS(),
        sa.UniqueConstraint("handover_id", "so_id", name="uq_handover_items_handover_so"),
    )
    op.create_index("ix_invoice_handover_items_handover_id", "invoice_handover_items", ["handover_id"])
    op.create_index("ix_invoice_handover_items_so_id", "invoice_handover_items", ["so_id"])
    op.create_index("ix_invoice_handover_items_officer_status", "invoice_handover_items", ["officer_status"])

    # --- DRAFT seeds (replaced by Logistics through the admin UI; each edit becomes a new version) ---
    # Plain escaped SQL (constants only) so the migration also renders with `alembic ... --sql`.
    op.execute(
        "INSERT INTO form_templates (id, kind, version, name, is_draft, active, source_note) VALUES "
        f"(1, 'acknowledge_checklist', 1, {_q('DRAFT — replace with Logistics checklist')}, true, true, {_q('Placeholder checklist seeded by the system; not yet confirmed by Logistics.')}), "
        f"(2, 'delivery_checkout', 1, {_q('DRAFT — mirror of Zoho form B2BLogisticsDelivery (field list not yet verified)')}, true, true, "
        f"{_q('The Zoho form page is not publicly readable, so these fields are a placeholder. Replace with the real field list.')})"
    )
    values = []
    for position, (key, label, item_type, required) in enumerate(ACK_ITEMS, 1):
        values.append(f"(1, {position}, {_q(key)}, {_q(label)}, {_q(item_type)}, {_b(required)}, NULL, NULL, NULL, true)")
    for position, (key, label, item_type, required, options, prefill, show_when) in enumerate(CHECKOUT_ITEMS, 1):
        values.append(f"(2, {position}, {_q(key)}, {_q(label)}, {_q(item_type)}, {_b(required)}, {_j(options)}, {_q(prefill) if prefill else 'NULL'}, {_j(show_when)}, true)")
    op.execute("INSERT INTO form_template_items (template_id, position, key, label, item_type, required, options, prefill, show_when, active) VALUES " + ", ".join(values))
    # explicit ids above do not advance the sequence
    op.execute("SELECT setval(pg_get_serial_sequence('form_templates', 'id'), (SELECT max(id) FROM form_templates))")


def downgrade() -> None:
    for table in ("invoice_handover_items", "invoice_handovers", "trip_photos", "trip_events", "trip_stops", "so_acknowledgements", "trips", "form_template_items", "form_templates"):
        op.drop_table(table)
