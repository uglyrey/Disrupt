"""Upgrading owners preserves credentials and sessions; downgrade must not elevate observers."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.db import make_database


def test_owner_sessions_survive_and_observers_block_unsafe_downgrade(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "observer-migration.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    config = Config("alembic.ini")
    command.upgrade(config, "e7b3c1d9a2f4")
    engine, _ = make_database(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO admin_accounts (id,username,password_hash,totp_secret,created_at) "
                        "VALUES ('owner-id','owner','synthetic-hash','synthetic-encrypted-secret',1)"))
        db.execute(text("INSERT INTO admin_sessions "
                        "(token_hash,admin_id,created_at,expires_at,last_seen_at,csrf_hash,ip) "
                        "VALUES ('synthetic-token','owner-id',1,9999999999,1,'synthetic-csrf','127.0.0.1')"))
    command.upgrade(config, "head")
    command.check(config)
    with engine.begin() as db:
        row = db.execute(text("SELECT role,access_expires_at,password_hash,totp_secret FROM admin_accounts")).one()
        assert row == ('owner', None, 'synthetic-hash', 'synthetic-encrypted-secret')
        assert db.execute(text("SELECT admin_id FROM admin_sessions")).scalar() == 'owner-id'
        db.execute(text("INSERT INTO admin_accounts (id,username,password_hash,totp_secret,created_at,role) "
                        "VALUES ('observer-id','observer','synthetic-hash','synthetic-secret',1,'observer')"))
    with pytest.raises(RuntimeError, match="observer"):
        command.downgrade(config, "e7b3c1d9a2f4")
    with engine.begin() as db:
        assert db.execute(text("SELECT role FROM admin_accounts WHERE id='observer-id'")).scalar() == 'observer'
        db.execute(text("DELETE FROM admin_accounts WHERE id='observer-id'"))
    command.downgrade(config, "e7b3c1d9a2f4")
    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()
