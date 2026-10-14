"""Колонки часового пояса: обновление и откат на заполненной базе."""

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.db import make_database


def test_upgrade_from_current_production_keeps_images_and_mail_consent(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "current-production.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "c2e4a6b8d0f1")
    engine, _ = make_database(url)
    try:
        with engine.begin() as db:
            db.execute(text(
                "INSERT INTO users (id,username,password_hash,live_calls,marketing_consent_at,"
                "marketing_consent_version,marketing_consent_source) VALUES ('u','legacy','hash',0,1,'v1','email')"
            ))
            db.execute(text(
                "INSERT INTO captures (id,user_id,original_text,idempotency_key,created_at) "
                "VALUES ('c','u','encrypted-fixture','capture',1)"
            ))
            db.execute(text(
                "INSERT INTO notes (id,user_id,capture_id,version,title,markdown,conclusions,provider,created_at,updated_at) "
                "VALUES ('n','u','c',1,'encrypted-title','encrypted-body','[]','manual',1,1)"
            ))
            db.execute(text(
                "INSERT INTO note_images (id,note_id,capture_id,idempotency_key,source_sha256,media_type,"
                "byte_size,width,height,created_at,data) VALUES ('i','n','c','image','digest','image/webp',7,10,10,1,:data)"
            ), {"data": b"sealed-image-fixture"})
        command.upgrade(config, "head")
        command.check(config)
        with engine.connect() as db:
            assert tuple(db.execute(text(
                "SELECT timezone,timezone_source,timezone_asked_at,marketing_consent_at,"
                "marketing_consent_version,marketing_consent_source FROM users"
            )).one()) == (None, None, None, 1, "v1", "email")
            assert tuple(db.execute(text("SELECT note_id,capture_id,data FROM note_images")).one()) == (
                "n", "c", b"sealed-image-fixture",
            )
            assert db.execute(text("SELECT original_text FROM captures")).scalar() == "encrypted-fixture"
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()


def test_account_timezone_columns_round_trip_and_keep_existing_rows(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "timezone.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    config = Config("alembic.ini")
    command.upgrade(config, "d7f9b2c4e6a8")
    engine, _ = make_database(url)
    try:
        with engine.begin() as db:
            db.execute(text("INSERT INTO users (id,username,password_hash,live_calls) VALUES ('u','legacy','hash',0)"))
            db.execute(text(
                "INSERT INTO captures (id,user_id,original_text,idempotency_key,created_at) VALUES ('c','u','text','key',1)"
            ))
            db.execute(text("INSERT INTO notes (id,user_id,capture_id,version,title,markdown,conclusions,provider,created_at,updated_at) "
                "VALUES ('n','u','c',1,'t','m','[]','mock',1,1)"))
            db.execute(text(
                "INSERT INTO reminders (id,user_id,note_id,scheduled_at,timezone,text,status,generation,confirmed_at) "
                "VALUES ('r','u','n',2000000000,'Europe/Moscow','x','confirmed',1,1)"
            ))
        command.upgrade(config, "head")
        command.check(config)
        with engine.connect() as db:
            row = db.execute(text("SELECT timezone,timezone_source,timezone_asked_at FROM users")).one()
            assert tuple(row) == (None, None, None)  # У старых аккаунтов нет выдуманного пояса.
            assert db.execute(text("SELECT zone_guessed FROM reminders")).scalar() in (0, False)
        command.downgrade(config, "d7f9b2c4e6a8")
        with engine.connect() as db:
            users = {item[1] for item in db.execute(text("PRAGMA table_info(users)")).all()}
            assert {"timezone", "timezone_source", "timezone_asked_at"}.isdisjoint(users)
            reminders = {item[1] for item in db.execute(text("PRAGMA table_info(reminders)")).all()}
            assert "zone_guessed" not in reminders
            assert db.execute(text("SELECT timezone FROM reminders")).scalar() == "Europe/Moscow"
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()
