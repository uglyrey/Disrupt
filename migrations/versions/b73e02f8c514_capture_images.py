"""Keep attachments with the original, including queued and failed processing."""

import sqlalchemy as sa
from alembic import op

revision = "b73e02f8c514"
down_revision = "a62d91e7b403"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("note_images", sa.Column("capture_id", sa.String(36), nullable=True))
    op.execute("UPDATE note_images SET capture_id = (SELECT capture_id FROM notes WHERE notes.id = note_images.note_id)")
    with op.batch_alter_table("note_images") as batch:
        batch.alter_column("capture_id", existing_type=sa.String(36), nullable=False)
        batch.alter_column("note_id", existing_type=sa.String(36), nullable=True)
        batch.create_foreign_key("fk_note_images_capture", "captures", ["capture_id"], ["id"], ondelete="CASCADE")
        batch.drop_constraint("uq_note_images_upload", type_="unique")
        batch.create_unique_constraint("uq_capture_images_upload", ["capture_id", "idempotency_key"])
        batch.create_index("ix_note_images_capture_id", ["capture_id"])


def downgrade():
    # A downgrade cannot represent pending attachments. Refuse instead of deleting originals.
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM note_images WHERE note_id IS NULL")):
        raise RuntimeError("Pending image originals must be resolved before downgrade")
    with op.batch_alter_table("note_images") as batch:
        batch.drop_index("ix_note_images_capture_id")
        batch.drop_constraint("uq_capture_images_upload", type_="unique")
        batch.create_unique_constraint("uq_note_images_upload", ["note_id", "idempotency_key"])
        batch.drop_constraint("fk_note_images_capture", type_="foreignkey")
        batch.alter_column("note_id", existing_type=sa.String(36), nullable=False)
        batch.drop_column("capture_id")
