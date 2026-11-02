"""Event length for the Google Calendar link: per reminder and a default per person."""

import sqlalchemy as sa
from alembic import op

revision = "e3a7c9b1d5f2"
down_revision = "c6e8a1b3d5f7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reminders", sa.Column("duration_minutes", sa.Integer(), nullable=True))
    op.add_column("reminders", sa.Column("note_line", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("calendar_default_minutes", sa.Integer(), server_default="60", nullable=False))


def downgrade():
    op.drop_column("users", "calendar_default_minutes")
    op.drop_column("reminders", "note_line")
    op.drop_column("reminders", "duration_minutes")
