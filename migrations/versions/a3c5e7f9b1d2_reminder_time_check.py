"""Проверка времени напоминания: пометка, второе прочтение и напоминания, о которых спрашивает сообщение бота."""
import sqlalchemy as sa
from alembic import op

revision = "a3c5e7f9b1d2"
down_revision = "f4b8d2a6c1e9"
branch_labels = None
depends_on = None


def upgrade():
    # Обычный ADD COLUMN: пересоздание таблиц ломается на внешних ключах SQLite.
    op.add_column("reminders", sa.Column("time_check", sa.String(length=16), nullable=True))
    op.add_column("reminders", sa.Column("time_alt", sa.Float(), nullable=True))
    op.add_column("outbox", sa.Column("message_reminders", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("outbox", "message_reminders")
    op.drop_column("reminders", "time_alt")
    op.drop_column("reminders", "time_check")
