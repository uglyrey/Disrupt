"""Separate, expiring observer accounts. Existing owners and sessions remain intact."""
import sqlalchemy as sa
from alembic import op

revision = "f8c42a19d063"
down_revision = "e7b3c1d9a2f4"
branch_labels = None
depends_on = None


def upgrade():
    # An inline check permits ADD COLUMN on SQLite without recreating a table referenced
    # by live sessions and audit rows. PostgreSQL supports the same additive statement.
    op.execute("ALTER TABLE admin_accounts ADD COLUMN role VARCHAR(16) DEFAULT 'owner' NOT NULL "
               "CONSTRAINT ck_admin_accounts_role CHECK (role IN ('owner', 'observer'))")
    op.add_column("admin_accounts", sa.Column("access_expires_at", sa.Float(), nullable=True))


def downgrade():
    # Refuse to turn observers into owners when rolling back a live installation.
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM admin_accounts WHERE role = 'observer'")).scalar():
        raise RuntimeError("Disable and remove observer accounts before dropping their role boundary")
    op.drop_column("admin_accounts", "access_expires_at")
    op.drop_column("admin_accounts", "role")
