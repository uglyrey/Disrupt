"""Часовой пояс аккаунта: пояс, откуда он взят, когда бот спросил, и пометка догаданных напоминаний."""
import sqlalchemy as sa
from alembic import op

revision = "e1a4c7f2b9d3"
down_revision = "d7f9b2c4e6a8"
branch_labels = None
depends_on = None


def upgrade():
    # Обычный ADD COLUMN: пересоздание таблиц ломается на внешних ключах SQLite.
    op.add_column("users", sa.Column("timezone", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("timezone_source", sa.String(length=16), nullable=True))
    op.add_column("users", sa.Column("timezone_asked_at", sa.Integer(), nullable=True))
    op.add_column("reminders", sa.Column("zone_guessed", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    # Обычный DROP COLUMN: пересоздание таблиц ломается на внешних ключах SQLite.
    op.drop_column("reminders", "zone_guessed")
    op.drop_column("users", "timezone_asked_at")
    op.drop_column("users", "timezone_source")
    op.drop_column("users", "timezone")
