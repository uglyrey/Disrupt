"""Automatic retries of AI processing. Columns of the job that waits for its next attempt."""
import sqlalchemy as sa
from alembic import op

revision = "a9d4e2f7c1b8"
down_revision = "e7b3c1d9a2f4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("not_before", sa.Float(), nullable=True))
        batch.add_column(sa.Column("last_error_code", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("last_failed_at", sa.Float(), nullable=True))
        batch.create_index("ix_jobs_last_failed_at", ["last_failed_at"])


def downgrade():
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_last_failed_at")
        batch.drop_column("last_failed_at")
        batch.drop_column("last_error_code")
        batch.drop_column("not_before")
        batch.drop_column("attempts")
