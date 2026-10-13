"""Proof of delivery is REQUIRED at check-out (2026-10-13, requested by Rishi). ADDITIVE: a new template version, nothing is edited or deleted.

Copies the active "Delivery time log" into the next version and changes only the photo items:
- delivery_photo  "Proof of delivery photo": required, shown only when outcome is Delivered or Partially delivered
- failure_photo   "Photo (optional)":        optional, shown only when outcome is Failed

The previous version is only flagged inactive, so every stop already filled stays readable. Skipped (no change) when the active template is not the
seeded one any more, e.g. an admin already saved their own version in Admin > Settings.
"""
from alembic import op

revision = "r8d9e0f1a2b3"
down_revision = "q7c8d9e0f1a2"
branch_labels = None
depends_on = None

SQL = r"""
DO $$
DECLARE
    old_id bigint;
    new_id bigint;
BEGIN
    SELECT t.id INTO old_id FROM form_templates t
    WHERE t.kind = 'delivery_checkout' AND t.active AND t.name = 'Delivery time log'
      AND EXISTS (SELECT 1 FROM form_template_items i WHERE i.template_id = t.id AND i.key = 'delivery_photo' AND NOT i.required AND i.show_when IS NULL)
      AND NOT EXISTS (SELECT 1 FROM form_template_items i WHERE i.template_id = t.id AND i.key = 'failure_photo')
    ORDER BY t.version DESC LIMIT 1;
    IF old_id IS NULL THEN
        RETURN;
    END IF;
    INSERT INTO form_templates (kind, version, name, is_draft, active, source_note)
    SELECT kind, (SELECT MAX(version) + 1 FROM form_templates WHERE kind = 'delivery_checkout'), name, false, true, source_note
    FROM form_templates WHERE id = old_id RETURNING id INTO new_id;
    UPDATE form_templates SET active = false WHERE kind = 'delivery_checkout' AND id <> new_id;
    INSERT INTO form_template_items (template_id, position, key, label, item_type, required, options, prefill, show_when, active, app_only)
    SELECT new_id,
           CASE i.key WHEN 'delivery_photo' THEN 4 WHEN 'notes' THEN 6 ELSE i.position END,
           i.key,
           CASE i.key WHEN 'delivery_photo' THEN 'Proof of delivery photo' ELSE i.label END,
           i.item_type,
           CASE i.key WHEN 'delivery_photo' THEN true ELSE i.required END,
           i.options, i.prefill,
           CASE i.key WHEN 'delivery_photo' THEN '{"key": "outcome", "in": ["Delivered", "Partially delivered"]}'::jsonb ELSE i.show_when END,
           i.active, i.app_only
    FROM form_template_items i WHERE i.template_id = old_id;
    INSERT INTO form_template_items (template_id, position, key, label, item_type, required, options, prefill, show_when, active, app_only)
    VALUES (new_id, 5, 'failure_photo', 'Photo (optional)', 'photo', false, NULL, NULL, '{"key": "outcome", "in": ["Failed"]}'::jsonb, true, true);
END
$$;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    # Data-only migration: older template versions are never deleted, so there is nothing to undo safely (stops filled with the new version keep their answers).
    pass
