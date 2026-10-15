"""Миграция входа по коду: обращения без аккаунта и пометка «пароля нет» у Telegram-аккаунтов."""

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.db import make_database

EVENT = (
    "INSERT INTO product_events (id,user_id,name,operation_id,channel,source,is_test,session_id,occurred_at,app_version,outcome) "
    "VALUES (:i,:u,:n,:i,'web','x',0,'s',1,'v',:o)"
)
FEEDBACK = (
    "INSERT INTO feedback (id,user_id,idempotency_key,payload_hash,kind,subject,description,steps,expected,contact,"
    "status,version,created_at,updated_at) VALUES (:i,:u,:k,'h',:kind,'s','d','','','c','new',1,1,1)"
)


def test_recovery_rows_and_unusable_passwords_round_trip(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "signin.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    config = Config("alembic.ini")
    command.upgrade(config, "e1a4c7f2b9d3")
    engine, _ = make_database(url)
    try:
        with engine.begin() as db:
            db.execute(text(
                "INSERT INTO users (id,username,password_hash,live_calls) VALUES "
                "('tg','tg1','random-hash',0),('pw','pw','scrypt$a$b',0),('reset','tg2','new-hash',0)"
            ))
            for user, name, outcome, key in [
                ("tg", "registered", "telegram", "1"), ("reset", "registered", "telegram", "2"),
                ("reset", "password_reset", None, "3"), ("pw", "registered", None, "4"),
            ]:
                db.execute(text(EVENT), dict(i=key, u=user, n=name, o=outcome))
            db.execute(text(FEEDBACK), dict(i="f", u="pw", k="k", kind="bug"))
        command.upgrade(config, "head")
        command.check(config)
        with engine.begin() as db:
            hashes = dict(db.execute(text("SELECT id,password_hash FROM users")).all())
            assert hashes == {"tg": "!", "pw": "scrypt$a$b", "reset": "new-hash"}
            db.execute(text(FEEDBACK), dict(i="r", u=None, k="k2", kind="recovery"))
            assert db.execute(text("SELECT count(*) FROM feedback")).scalar() == 2
        command.downgrade(config, "e1a4c7f2b9d3")
        with engine.connect() as db:
            assert db.execute(text("SELECT id FROM feedback")).scalars().all() == ["f"]
            assert db.execute(text("SELECT password_hash FROM users WHERE id='tg'")).scalar().startswith("scrypt$")
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()


def test_upgrade_from_installed_production_head_preserves_mail_consent(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "production-signin.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    config = Config("alembic.ini")
    command.upgrade(config, "f8c3d1a5b902")
    engine, _ = make_database(url)
    try:
        with engine.begin() as db:
            db.execute(text(
                "INSERT INTO users (id,username,password_hash,email,email_verified_at,live_calls,"
                "marketing_consent_at,marketing_consent_source) VALUES "
                "('mail','mailuser','scrypt$a$b','owner@example.test',1,0,123,'email-registration')"
            ))
        command.upgrade(config, "head")
        command.check(config)
        with engine.connect() as db:
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar() == ScriptDirectory.from_config(config).get_current_head()
            assert db.execute(text(
                "SELECT email,marketing_consent_at,marketing_consent_source FROM users WHERE id='mail'"
            )).one() == ("owner@example.test", 123, "email-registration")
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()
