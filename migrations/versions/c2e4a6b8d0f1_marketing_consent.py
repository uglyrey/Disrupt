"""Record an optional, revocable consent to Beresta messages."""

import sqlalchemy as sa
from alembic import op

revision = "c2e4a6b8d0f1"
down_revision = "b73e02f8c514"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("marketing_consent_at", sa.Float(), nullable=True))
    op.add_column("users", sa.Column("marketing_consent_version", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("marketing_consent_source", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("marketing_consent_revoked_at", sa.Float(), nullable=True))
    op.add_column("telegram_logins", sa.Column("marketing_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("email_registration_pending", sa.Column("marketing_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("email_registration_pending", "marketing_opt_in")
    op.drop_column("telegram_logins", "marketing_opt_in")
    op.drop_column("users", "marketing_consent_revoked_at")
    op.drop_column("users", "marketing_consent_source")
    op.drop_column("users", "marketing_consent_version")
    op.drop_column("users", "marketing_consent_at")
