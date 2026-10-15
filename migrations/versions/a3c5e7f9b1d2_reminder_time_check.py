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
    # Письменный ответ на «Изменить время», который разборщик не понял. API только кладёт строку, ИИ зовёт воркер.
    op.create_table(
        "time_requests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reminder_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("lease_until", sa.Float(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.UniqueConstraint("bot_id", "update_id", name="uq_time_requests_update"),
        sa.CheckConstraint("status IN ('queued', 'running', 'done', 'failed')", name="ck_time_requests_status"),
    )
    op.create_index("ix_time_requests_user_id", "time_requests", ["user_id"])
    op.create_index("ix_time_requests_status_created", "time_requests", ["status", "created_at"])


def downgrade():
    op.drop_index("ix_time_requests_status_created", table_name="time_requests")
    op.drop_index("ix_time_requests_user_id", table_name="time_requests")
    op.drop_table("time_requests")
    op.drop_column("outbox", "message_reminders")
    op.drop_column("reminders", "time_alt")
    op.drop_column("reminders", "time_check")
