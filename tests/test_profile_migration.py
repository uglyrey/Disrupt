"""Миграция b5d7f9a1c3e4: профиль, переключатель календаря, ранние напоминания и категория «Общее»."""

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.db import make_database

BEFORE = "a3c5e7f9b1d2"


def seed(db):
    for user in ("a", "b", "c"):
        db.execute(text(f"INSERT INTO users (id,username,password_hash,live_calls) VALUES ('{user}','{user}','hash',0)"))
        db.execute(text(f"INSERT INTO captures (id,user_id,original_text,idempotency_key,created_at) VALUES ('c{user}','{user}','t','k{user}',1)"))
    db.execute(text("INSERT INTO categories (id,user_id,name,version) VALUES ('own','b','общее',3), ('work','c','Работа',1)"))
    for note, user, category, deleted in (("na", "a", None, None), ("nb", "b", None, None), ("nc", "c", "work", None), ("nd", "a", None, 5)):
        capture = "c" + user if note != "nd" else "ca"
        if note == "nd":
            db.execute(text("INSERT INTO captures (id,user_id,original_text,idempotency_key,created_at) VALUES ('cd','a','t','kd',1)"))
            capture = "cd"
        db.execute(text(
            "INSERT INTO notes (id,user_id,capture_id,version,title,markdown,conclusions,provider,created_at,updated_at,"
            f"category_id,deleted_at) VALUES ('{note}','{user}','{capture}',1,'t','m','[]','mock',1,1,"
            f"{'NULL' if category is None else repr(category)},{'NULL' if deleted is None else deleted})"
        ))


def test_general_category_and_profile_columns_round_trip_on_a_filled_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "profile.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    config = Config("alembic.ini")
    command.upgrade(config, BEFORE)
    engine, _ = make_database(url)
    try:
        with engine.begin() as db:
            seed(db)
        command.upgrade(config, "head")
        command.check(config)
        with engine.connect() as db:
            general = {row.user_id: row for row in db.execute(text("SELECT id,user_id,name,version,is_general FROM categories WHERE is_general"))}
            assert set(general) == {"a", "b", "c"}  # Exactly one each (a second would break the dict)
            assert db.execute(text("SELECT COUNT(*) FROM categories WHERE is_general")).scalar() == 3
            assert general["b"].id == "own" and general["b"].name == "Общее" and general["b"].version == 3  # Adopted
            notes = {row.id: row.category_id for row in db.execute(text("SELECT id,category_id FROM notes"))}
            assert notes == {"na": general["a"].id, "nd": general["a"].id, "nb": "own", "nc": "work"}
            assert db.execute(text("SELECT calendar_offer FROM users")).scalars().all() == [1, 1, 1]
            assert db.execute(text("SELECT COUNT(*) FROM user_profiles")).scalar() == 0
            assert db.execute(text("SELECT calendar_default_minutes FROM users")).scalars().all() == [60, 60, 60]
            assert "duration_minutes" in {item[1] for item in db.execute(text("PRAGMA table_info(reminders)")).all()}
            assert "advance_of" in {item[1] for item in db.execute(text("PRAGMA table_info(reminders)")).all()}
        command.downgrade(config, BEFORE)
        with engine.connect() as db:
            assert {"calendar_offer"}.isdisjoint({item[1] for item in db.execute(text("PRAGMA table_info(users)")).all()})
            assert "is_general" not in {item[1] for item in db.execute(text("PRAGMA table_info(categories)")).all()}
            assert "advance_of" not in {item[1] for item in db.execute(text("PRAGMA table_info(reminders)")).all()}
            assert "duration_minutes" not in {item[1] for item in db.execute(text("PRAGMA table_info(reminders)")).all()}
            tables = {row[0] for row in db.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
            assert "user_profiles" not in tables
            assert db.execute(text("SELECT id,category_id FROM notes ORDER BY id")).all() == [
                ("na", None), ("nb", None), ("nc", "work"), ("nd", None),
            ]
            assert db.execute(text("SELECT id FROM categories")).scalars().all() == ["work"]
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        command.upgrade(config, "head")
        command.check(config)
        with engine.connect() as db:
            assert db.execute(text("SELECT COUNT(*) FROM categories WHERE is_general")).scalar() == 3
    finally:
        engine.dispose()
