"""Профиль «Знакомство», предложение календаря, ранние напоминания и категория «Общее»."""
import uuid

import sqlalchemy as sa
from alembic import op

revision = "b5d7f9a1c3e4"
down_revision = "a3c5e7f9b1d2"
branch_labels = None
depends_on = None

GENERAL = "Общее"


def key(name):
    return name.casefold().replace("ё", "е")


def upgrade():
    # Обычный ADD COLUMN: пересоздание таблиц ломается на внешних ключах SQLite.
    op.add_column("users", sa.Column("calendar_offer", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column("categories", sa.Column("is_general", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("reminders", sa.Column("advance_of", sa.String(length=36), nullable=True))
    op.create_index("ix_reminders_advance_of", "reminders", ["advance_of"])
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=True),
        sa.Column("facts", sa.JSON(), nullable=True),
        sa.Column("prefs", sa.JSON(), nullable=True),
        sa.Column("answers_hash", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.Float(), nullable=True),
        sa.Column("unit_times", sa.JSON(), nullable=True),
        sa.Column("categories_confirmed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint("state IN ('declined', 'pending', 'ready', 'failed')", name="ck_user_profiles_state"),
    )
    op.create_index("ix_user_profiles_state_lease", "user_profiles", ["state", "lease_until"])
    general_categories()


def general_categories():
    """«Общее» есть у каждого. Своя категория с таким именем становится общей, заметки без категории переезжают в неё."""
    connection = op.get_bind()
    users = sa.table("users", sa.column("id"))
    categories = sa.table(
        "categories", sa.column("id"), sa.column("user_id"), sa.column("name"), sa.column("version"),
        sa.column("is_general"),
    )
    notes = sa.table("notes", sa.column("user_id"), sa.column("category_id"))
    existing = {}
    for row in connection.execute(sa.select(categories.c.id, categories.c.user_id, categories.c.name).order_by(categories.c.id)):
        if key(row.name.strip()) == key(GENERAL):
            existing.setdefault(row.user_id, row.id)
    for user_id in connection.execute(sa.select(users.c.id)).scalars().all():
        category_id = existing.get(user_id)
        if category_id is None:
            category_id = str(uuid.uuid4())
            connection.execute(categories.insert().values(
                id=category_id, user_id=user_id, name=GENERAL, version=1, is_general=True,
            ))
        else:
            connection.execute(
                categories.update().where(categories.c.id == category_id).values(is_general=True, name=GENERAL)
            )
        connection.execute(
            notes.update().where(notes.c.user_id == user_id, notes.c.category_id.is_(None)).values(category_id=category_id)
        )


def downgrade():
    connection = op.get_bind()
    categories = sa.table("categories", sa.column("id"), sa.column("is_general"))
    notes = sa.table("notes", sa.column("category_id"))
    items = sa.table("items", sa.column("category_id"))
    general = sa.select(categories.c.id).where(categories.c.is_general.is_(True))
    # Заметки снова без категории, а «Общее» уходит: до миграции такой категории у всех не было.
    connection.execute(notes.update().where(notes.c.category_id.in_(general)).values(category_id=None))
    connection.execute(items.update().where(items.c.category_id.in_(general)).values(category_id=None))
    connection.execute(categories.delete().where(categories.c.is_general.is_(True)))
    op.drop_index("ix_user_profiles_state_lease", table_name="user_profiles")
    op.drop_table("user_profiles")
    op.drop_index("ix_reminders_advance_of", table_name="reminders")
    op.drop_column("reminders", "advance_of")
    op.drop_column("categories", "is_general")
    op.drop_column("users", "calendar_offer")
