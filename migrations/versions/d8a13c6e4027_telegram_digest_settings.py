"""Opt-in morning and evening Telegram recaps."""

import sqlalchemy as sa
from alembic import op

revision = "d8a13c6e4027"
down_revision = "b6a29f8d3104"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telegram_digest_settings",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("morning_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("evening_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("morning_time", sa.String(5), server_default="09:00", nullable=False),
        sa.Column("evening_time", sa.String(5), server_default="20:00", nullable=False),
        sa.Column("timezone", sa.String(64), server_default="Europe/Moscow", nullable=False),
        sa.Column("generation", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_digest_settings_generation"),
    )


def downgrade():
    op.drop_table("telegram_digest_settings")
