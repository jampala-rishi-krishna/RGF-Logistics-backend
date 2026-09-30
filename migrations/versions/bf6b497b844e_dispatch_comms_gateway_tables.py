"""dispatch comms gateway tables

Revision ID: bf6b497b844e
Revises: c4e7a1f0b2d3
"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "bf6b497b844e"
down_revision = "c4e7a1f0b2d3"
branch_labels = None
depends_on = None

# Roster provided in the Dispatch Communications Gateway spec - real data, seeded directly
# rather than left for manual entry.
STAFF_ROSTER = [
    ("Leo Barrameda", "Logistics Fleet Officer / Control Tower", "METS", "leo@rareglobalfood.com", "09778395378"),
    ("John Louis De Vera", "Logistics Officer", "METS", "louis@rareglobalfood.com", "09172467691"),
    ("Ivan James Muñoz", "Logistics Associate", "METS", "ivan.munoz@rareglobalfood.com", "09173150859"),
    ("Mark John Paul Cuyana", "Dispatcher", "METS", "johnpaulcuyana@gmail.com", "09705688911"),
    ("Nichole Durolfo", "Dispatcher", "METS", "nicho@rareglobalfood.com", "09171838808"),
    ("Melvin Yabut", "Dispatcher", "METS", "melvin@rareglobalfood.com", "09171063349"),
    ("John Lloyd Lopez", "Dispatcher", "GLACIER", "lloyd@rareglobalfood.com", "09948937952"),
    ("Kenneth Sanguyo", "Dispatcher", "GLACIER", "kenethsanguyo16@gmail.com", "09927145038"),
    ("Emert Hirondo", "MC Rider", "GLACIER", "odnorihpedz@gmail.com", "09482834802"),
    ("Paul Aljecera", "MC Rider", "METS", "paulmarkaljecera@gmail.com", "09367496403"),
    ("Jose De Mesa Jr", "MC Rider", "METS", "joedmesa27@gmail.com", "09159789443"),
    ("Mariano Arca", "Delivery Driver", "METS", "arcamariano19@gmail.com", "09770490509"),
    ("Albert Atienza", "Delivery Driver", "METS", "aatienza121294@gmail.com", "09935859476"),
    ("Roger Baluyot", "Delivery Driver", "METS", "rogerbaluyut0@gmail.com", "09648615733"),
    ("Gerardo Magisa", "Delivery Driver", "METS", "magisagerardo78@gmail.com", "09278146574"),
    ("Norman Manalang", "Delivery Driver", "METS", "manalangnorman26@gmail.com", "09055991200"),
    ("Raymond Roblo", "Delivery Helper", "METS", "raymondroblo94@gmail.com", "09124297344"),
    ("Jaymar Arca", "Delivery Helper", "METS", "jaymararca07@gmail.com", "09692045697"),
    ("Bryan San. Antonio", "Delivery Helper", "METS", "bryan.sanantonio21@gmail.com", "09755350376"),
    ("Joshua Luna", "Delivery Helper", "METS", "lunajoshua0015@gmail.com", "09955092130"),
    ("Andy Lira", "Delivery Helper", "METS", "liraandy14@gmail.com", "09812878440"),
]


def upgrade() -> None:
    op.create_table(
        "staff_directory",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("warehouse", sa.Text()),
        sa.Column("email", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "customer_contacts",
        sa.Column("zoho_customer_id", sa.String(length=200)),
        sa.Column("customer_name", sa.Text()),
        sa.Column("email", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("whatsapp_number", sa.Text()),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_customer_contacts_zoho_customer_id"), "customer_contacts", ["zoho_customer_id"], unique=False)

    op.create_table(
        "message_templates",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("audience", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text()),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "message_log",
        sa.Column("audience", sa.Text(), nullable=False),
        sa.Column("recipient_name", sa.Text()),
        sa.Column("recipient_contact", sa.Text()),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("template_name", sa.Text()),
        sa.Column("trigger_event", sa.Text()),
        sa.Column("related_so_number", sa.Text()),
        sa.Column("body", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("provider_message_id", sa.Text()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_message_log_related_so_number"), "message_log", ["related_so_number"], unique=False)

    now = datetime.now(timezone.utc)
    staff_table = sa.table(
        "staff_directory",
        sa.column("name", sa.Text()),
        sa.column("title", sa.Text()),
        sa.column("warehouse", sa.Text()),
        sa.column("email", sa.Text()),
        sa.column("phone", sa.Text()),
        sa.column("active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        staff_table,
        [
            {
                "name": name,
                "title": title,
                "warehouse": warehouse,
                "email": email,
                "phone": phone,
                "active": True,
                "created_at": now,
                "updated_at": now,
            }
            for name, title, warehouse, email, phone in STAFF_ROSTER
        ],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_message_log_related_so_number"), table_name="message_log")
    op.drop_table("message_log")
    op.drop_table("message_templates")
    op.drop_index(op.f("ix_customer_contacts_zoho_customer_id"), table_name="customer_contacts")
    op.drop_table("customer_contacts")
    op.drop_table("staff_directory")
