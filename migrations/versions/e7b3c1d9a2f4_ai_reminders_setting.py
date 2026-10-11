"""Switch for reminders that the AI sets by itself. On for everyone, also for existing users."""
import sqlalchemy as sa
from alembic import op

revision = "e7b3c1d9a2f4"
down_revision = "d6a18f9c2e40"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column(
        "ai_reminders_enabled", sa.Boolean(), server_default=sa.true(), nullable=False,
    ))


def downgrade():
    op.drop_column("users", "ai_reminders_enabled")
