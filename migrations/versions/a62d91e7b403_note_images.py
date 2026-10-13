"""Private encrypted images in notes."""

import sqlalchemy as sa
from alembic import op

revision = "a62d91e7b403"
down_revision = "f5a18b7c9d20"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "note_images",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("note_id", sa.String(36), sa.ForeignKey("notes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("media_type", sa.String(32), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint("note_id", "idempotency_key", name="uq_note_images_upload"),
    )
    op.create_index("ix_note_images_note_id", "note_images", ["note_id"])


def downgrade():
    op.drop_index("ix_note_images_note_id", table_name="note_images")
    op.drop_table("note_images")
