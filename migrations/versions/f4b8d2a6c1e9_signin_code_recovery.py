"""Вход по коду: обращения «Восстановить аккаунт» без аккаунта и пометка «пароля нет»."""
import base64

import sqlalchemy as sa
from alembic import op

revision = "f4b8d2a6c1e9"
down_revision = "e1a4c7f2b9d3"
branch_labels = None
depends_on = None

OLD_KINDS = "kind IN ('bug','idea','question','other')"
NEW_KINDS = "kind IN ('bug','idea','question','other','recovery')"
# Верный по форме хеш, под который не подобрать пароль: так откат не ломает старый код проверки.
FILLER = "scrypt$" + base64.b64encode(bytes(16)).decode() + "$" + base64.b64encode(bytes(32)).decode()


def upgrade():
    with op.batch_alter_table("feedback", recreate="always") as batch:
        batch.alter_column("user_id", existing_type=sa.String(36), nullable=True)
        batch.drop_constraint("ck_feedback_kind", type_="check")
        batch.create_check_constraint("ck_feedback_kind", NEW_KINDS)
    # Аккаунты, созданные через Telegram, не знают своего пароля, если ни разу не сбрасывали его.
    op.execute(
        "UPDATE users SET password_hash = '!' WHERE id IN "
        "(SELECT user_id FROM product_events WHERE name = 'registered' AND outcome = 'telegram') "
        "AND id NOT IN (SELECT user_id FROM product_events WHERE name = 'password_reset')"
    )


def downgrade():
    op.execute(sa.text("UPDATE users SET password_hash = :filler WHERE password_hash = '!'").bindparams(filler=FILLER))
    op.execute("DELETE FROM feedback WHERE user_id IS NULL OR kind = 'recovery'")
    with op.batch_alter_table("feedback", recreate="always") as batch:
        batch.drop_constraint("ck_feedback_kind", type_="check")
        batch.create_check_constraint("ck_feedback_kind", OLD_KINDS)
        batch.alter_column("user_id", existing_type=sa.String(36), nullable=False)
