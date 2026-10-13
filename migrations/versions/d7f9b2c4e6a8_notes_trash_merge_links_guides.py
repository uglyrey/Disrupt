"""Корзина заметок и задач, объединение заметок, ручные связи, состояние подсказок."""
import sqlalchemy as sa
from alembic import op

revision = "d7f9b2c4e6a8"
down_revision = "6a1e4c8d9b20"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("notes") as batch:
        batch.add_column(sa.Column("deleted_at", sa.Float(), nullable=True))
        batch.add_column(sa.Column("merged_into", sa.String(length=36), nullable=True))
        batch.create_index("ix_notes_deleted_at", ["deleted_at"])
    with op.batch_alter_table("items") as batch:
        batch.add_column(sa.Column("deleted_at", sa.Float(), nullable=True))
        batch.create_index("ix_items_deleted_at", ["deleted_at"])
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("kind", sa.String(length=16), server_default="capture", nullable=False))
        batch.add_column(sa.Column("merge_note_ids", sa.JSON(), nullable=True))
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("guide_state", sa.JSON(), nullable=True))
    op.create_table(
        "note_links",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("note_a", sa.String(length=36), nullable=False),
        sa.Column("note_b", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint("note_a < note_b", name="ck_note_links_order"),
        sa.ForeignKeyConstraint(["note_a"], ["notes.id"]),
        sa.ForeignKeyConstraint(["note_b"], ["notes.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("note_a", "note_b", name="uq_note_links_pair"),
    )
    op.create_index("ix_note_links_user_id", "note_links", ["user_id"])
    op.create_index("ix_note_links_note_a", "note_links", ["note_a"])
    op.create_index("ix_note_links_note_b", "note_links", ["note_b"])


def downgrade():
    op.drop_index("ix_note_links_note_b", table_name="note_links")
    op.drop_index("ix_note_links_note_a", table_name="note_links")
    op.drop_index("ix_note_links_user_id", table_name="note_links")
    op.drop_table("note_links")
    # Обычный DROP COLUMN: пересоздание таблиц ломается на внешних ключах SQLite.
    op.drop_column("users", "guide_state")
    op.drop_column("jobs", "merge_note_ids")
    op.drop_column("jobs", "kind")
    op.drop_index("ix_items_deleted_at", table_name="items")
    op.drop_column("items", "deleted_at")
    op.drop_index("ix_notes_deleted_at", table_name="notes")
    op.drop_column("notes", "merged_into")
    op.drop_column("notes", "deleted_at")
