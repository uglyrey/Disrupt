"""Durable content-free component evidence and registration attribution."""

import sqlalchemy as sa
from alembic import op

revision = "a4c7e9b12d30"
down_revision = "f8c42a19d063"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("telegram_logins", "email_registration_pending"):
        op.add_column(table, sa.Column("source", sa.String(100), server_default="unknown", nullable=False))
    op.create_table(
        "component_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation_id", sa.String(100), nullable=False),
        sa.Column("component", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("is_test", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("occurred_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("user_id", "operation_id", "component", name="uq_component_call_stage"),
        sa.CheckConstraint(
            "kind IN ('skill', 'background', 'tool', 'target')", name="ck_component_call_kind"
        ),
    )
    op.create_index("ix_component_calls_user_id", "component_calls", ["user_id"])
    op.create_index("ix_component_calls_occurred_at", "component_calls", ["occurred_at"])
    op.create_table(
        "request_errors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("route", sa.String(160), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(32), nullable=False),
        sa.Column("is_test", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("occurred_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_request_errors_user_id", "request_errors", ["user_id"])
    op.create_index("ix_request_errors_occurred_at", "request_errors", ["occurred_at"])


def downgrade():
    op.drop_table("request_errors")
    op.drop_table("component_calls")
    for table in ("telegram_logins", "email_registration_pending"):
        op.drop_column(table, "source")
