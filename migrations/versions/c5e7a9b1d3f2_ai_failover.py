"""Резервный ИИ. Состояние основного шлюза, дневной расход резерва, сервис в учёте вызовов и в задании."""
import sqlalchemy as sa
from alembic import op

revision = "c5e7a9b1d3f2"
down_revision = "a9d4e2f7c1b8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "provider_routes",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=8), server_default="up", nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_failure_at", sa.Float(), nullable=True),
        sa.Column("changed_at", sa.Float(), nullable=True),
        sa.Column("next_probe_at", sa.Float(), nullable=True),
        sa.Column("probe_until", sa.Float(), nullable=True),
        sa.CheckConstraint("state IN ('up', 'down')", name="ck_provider_routes_state"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "fallback_spend",
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("committed_micro_usd", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("day"),
    )
    with op.batch_alter_table("provider_usage") as batch:
        batch.add_column(sa.Column("provider", sa.String(length=16), nullable=True))
    # Старые записи делал основной шлюз, имитация помечена моделью mock.
    op.execute("UPDATE provider_usage SET provider = CASE WHEN model = 'mock' THEN 'mock' ELSE 'cloudru' END")
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("provider_used", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("next_route", sa.String(length=16), nullable=True))


def downgrade():
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("next_route")
        batch.drop_column("provider_used")
    with op.batch_alter_table("provider_usage") as batch:
        batch.drop_column("provider")
    op.drop_table("fallback_spend")
    op.drop_table("provider_routes")
