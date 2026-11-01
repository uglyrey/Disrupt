"""Продолжение заметки: запись можно добавить в уже существующую заметку (docs/attach-to-note-v1.md)."""

import sqlalchemy as sa
from alembic import op

revision = "a9c2e4f6b8d0"
down_revision = "e3a7c9b1d5f2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("captures", sa.Column("attach_state", sa.String(16), nullable=True))
    op.add_column("captures", sa.Column("attach_note_id", sa.String(36), nullable=True))
    op.add_column("captures", sa.Column("attach_explicit", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("captures", sa.Column("attach_offered_at", sa.Float(), nullable=True))
    op.add_column("captures", sa.Column("attach_decided_at", sa.Float(), nullable=True))
    op.add_column("captures", sa.Column("attach_heading", sa.String(64), nullable=True))
    op.add_column("captures", sa.Column("attach_moved", sa.JSON(), nullable=True))
    op.create_index("ix_captures_attach_note_id", "captures", ["attach_note_id"])


def downgrade():
    op.drop_index("ix_captures_attach_note_id", table_name="captures")
    for name in ("attach_moved", "attach_heading", "attach_decided_at", "attach_offered_at", "attach_explicit",
                 "attach_note_id", "attach_state"):
        op.drop_column("captures", name)
