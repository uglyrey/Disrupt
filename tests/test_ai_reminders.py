"""Reminders the AI sets by itself, and the three buttons under a delivered reminder. Mock provider, no network."""

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.db import make_database
from app.deliveries import clip, reminder_message_text
from app.models import Item, Job, Note, Outbox, Reminder, TelegramIdentity, User
from app.providers import MockProvider
from app.reminders import AUTO_KEY_PREFIX, auto_reminders, snooze_time
from app.scheduler import Scheduler
from app.worker import Worker
from tests.conftest import register
from tests.test_internal import BASE, HEADERS, message
from tests.test_processing_replies import processed
from tests.test_reminders import add, claim
from tests.test_reminders import reminders as reminders
from tests.test_telegram_actions import action
from tests.test_telegram_actions import task as task

MOSCOW = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")
SETTINGS = "/api/v1/assistant/settings"


def auto_rows(app):
    with app.state.sessions() as db:
        return db.scalars(select(Reminder).where(
            Reminder.idempotency_key.like(AUTO_KEY_PREFIX + "%")
        ).order_by(Reminder.scheduled_at, Reminder.id)).all()


def run_auto(app, note_id):
    with app.state.sessions.begin() as db:
        created = auto_reminders(db, db.get(Note, note_id), app.state.settings)
        return [row.id for row in created]


# Setting

def test_setting_is_on_by_default_and_can_be_turned_off_separately(app, client):
    register(client)
    assert client.get(SETTINGS).json() == {"recommendations_enabled": True, "ai_reminders_enabled": True}
    off = client.patch(SETTINGS, json={"ai_reminders_enabled": False})
    assert off.status_code == 200
    assert off.json() == {"recommendations_enabled": True, "ai_reminders_enabled": False}
    both = client.patch(SETTINGS, json={"recommendations_enabled": False, "ai_reminders_enabled": True})
    assert both.json() == {"recommendations_enabled": False, "ai_reminders_enabled": True}
    assert client.patch(SETTINGS, json={}).status_code == 422
    assert client.patch(SETTINGS, json={"ai_reminders_enabled": "no"}).status_code == 422
    assert client.patch(SETTINGS, json={"ai_reminders_enabled": None}).status_code == 422
    assert client.patch(SETTINGS, json={"unknown": True}).status_code == 422


def test_setting_belongs_to_one_user(app, client):
    register(client)
    with TestClient(app) as other:
        register(other, "second")
        other.patch(SETTINGS, json={"ai_reminders_enabled": False})
        assert other.get(SETTINGS).json()["ai_reminders_enabled"] is False
    assert client.get(SETTINGS).json()["ai_reminders_enabled"] is True


def test_migration_adds_the_setting_on_for_existing_users_and_downgrades(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "ai-reminders.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    config = Config("alembic.ini")
    command.upgrade(config, "d6a18f9c2e40")
    engine, sessions = make_database(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO users (id, username, password_hash, live_calls) VALUES ('u','old','hash',0)"))
    command.upgrade(config, "head")
    command.check(config)
    with sessions() as db:
        assert db.get(User, "u").ai_reminders_enabled is True  # Existing users get the feature.
    command.downgrade(config, "d6a18f9c2e40")
    with engine.connect() as db:
        columns = {row[1] for row in db.execute(text("PRAGMA table_info('users')"))}
        assert "ai_reminders_enabled" not in columns and "assistant_recommendations_enabled" in columns
        assert db.execute(text("SELECT username FROM users")).scalar() == "old"
    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()


# The worker sets reminders

def test_worker_sets_a_reminder_for_a_task_with_a_deadline(reminders):
    app, client, ids = reminders
    saved = processed(app, client)
    tomorrow = datetime.now(MOSCOW).date() + timedelta(days=1)
    (row,) = auto_rows(app)
    assert row.status == "confirmed" and row.generation == 1 and row.timezone == "Europe/Moscow"
    assert row.text == "Андрей завтра уточнит ситуацию."
    assert datetime.fromtimestamp(row.scheduled_at, MOSCOW) == datetime(
        tomorrow.year, tomorrow.month, tomorrow.day, 9, 0, tzinfo=MOSCOW,
    )
    with app.state.sessions() as db:
        note = db.scalar(select(Note).where(Note.capture_id == saved["capture_id"]))
        item = db.get(Item, row.item_id)
        assert row.note_id == note.id and item.kind == "task" and item.status == "open"
    # It is an ordinary reminder on the site, with no proposal left for the same task.
    listed = client.get(f"/api/v1/notes/{row.note_id}/reminders").json()
    assert [r["id"] for r in listed] == [row.id] and listed[0]["status"] == "confirmed"
    shown = client.get(f"/api/v1/notes/{row.note_id}").json()
    assert all(i["proposed_reminder"] is None for i in shown["items"])
    edit = {"scheduled_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(), "timezone": "Europe/Moscow",
            "text": "Позвонить Андрею", "generation": 1}
    assert client.patch(f"/api/v1/reminders/{row.id}", json=edit).json()["generation"] == 2
    cancelled = client.post(f"/api/v1/reminders/{row.id}/cancel", json={"generation": 2})
    assert cancelled.json()["status"] == "cancelled"


def test_telegram_reply_says_the_reminder_was_set(reminders):
    app, client, ids = reminders
    processed(app, client)
    (row,) = auto_rows(app)
    delivery = claim(client).json()["items"][0]
    assert delivery["text"].split("\n")[-1] == (
        "Напоминание поставлено: завтра в 09:00, «Андрей завтра уточнит ситуацию.». "
        f"Изменить: {delivery['note_url']}&reminder={row.id}"
    )
    assert "Предлагаю" not in delivery["text"] and delivery["callback_token"] is None
    assert len(delivery["text"].encode("utf-16-le")) <= 4096 * 2


def test_telegram_reply_is_a_proposal_again_when_the_setting_is_off(reminders):
    app, client, ids = reminders
    assert client.patch(SETTINGS, json={"ai_reminders_enabled": False}).status_code == 200
    processed(app, client)
    assert auto_rows(app) == []
    reply = claim(client).json()["items"][0]["text"]
    assert reply.split("\n")[-1].startswith("Предлагаю напоминание: завтра в 09:00")
    assert reply.endswith("#remind")


def test_cancelled_reminder_is_not_made_again(reminders):
    app, client, ids = reminders
    saved = processed(app, client)
    (row,) = auto_rows(app)
    assert client.post(f"/api/v1/reminders/{row.id}/cancel", json={"generation": 1}).status_code == 200
    with app.state.sessions() as db:
        note_id = db.scalar(select(Note.id).where(Note.capture_id == saved["capture_id"]))
    assert run_auto(app, note_id) == []
    assert len(auto_rows(app)) == 1
    assert "Напоминание поставлено" not in claim(client).json()["items"][0]["text"]


def test_setting_reminders_twice_makes_one(reminders):
    app, client, ids = reminders
    saved = processed(app, client)
    with app.state.sessions() as db:
        note_id = db.scalar(select(Note.id).where(Note.capture_id == saved["capture_id"]))
    assert run_auto(app, note_id) == []
    assert run_auto(app, note_id) == []
    assert len(auto_rows(app)) == 1


@pytest.mark.parametrize("due_text", [None, "", "скоро", "на следующей неделе", "вчера", "9 октября 2020"])
def test_no_reminder_without_a_readable_future_deadline(reminders, due_text):
    app, client, ids = reminders
    with app.state.sessions.begin() as db:
        db.get(Item, ids["item"]).due_text = due_text
    assert run_auto(app, ids["note"]) == []
    assert auto_rows(app) == []


def test_only_open_tasks_get_a_reminder(reminders):
    app, client, ids = reminders
    with app.state.sessions.begin() as db:
        db.get(Item, ids["item"]).due_text = "завтра в 10"
        db.add(Item(user_id=ids["user"], note_id=ids["note"], kind="idea", text="Идея", due_text="завтра"))
        db.add(Item(user_id=ids["user"], note_id=ids["note"], kind="task", text="Готово", due_text="завтра",
                    status="completed"))
    assert len(run_auto(app, ids["note"])) == 1
    (row,) = auto_rows(app)
    assert row.item_id == ids["item"] and row.text == "Позвонить"
    assert datetime.fromtimestamp(row.scheduled_at, MOSCOW).strftime("%H:%M") == "10:00"


def test_a_task_that_already_has_a_reminder_gets_no_second_one(reminders):
    app, client, ids = reminders
    with app.state.sessions.begin() as db:
        db.get(Item, ids["item"]).due_text = "завтра"
    assert add(client, ids).status_code == 201
    assert run_auto(app, ids["note"]) == []
    assert auto_rows(app) == []


def test_nothing_is_set_when_the_user_turned_it_off(reminders):
    app, client, ids = reminders
    with app.state.sessions.begin() as db:
        db.get(Item, ids["item"]).due_text = "завтра"
        db.get(User, ids["user"]).ai_reminders_enabled = False
    assert run_auto(app, ids["note"]) == []
    assert auto_rows(app) == []


def test_a_failure_while_setting_reminders_keeps_the_note(reminders, monkeypatch):
    app, client, ids = reminders
    saved = message(client, update_id=200).json()

    def broken(*args, **kwargs):
        raise RuntimeError("private text must not escape")

    monkeypatch.setattr("app.worker.auto_reminders", broken)
    assert Worker(app.state.sessions, app.state.settings, provider=MockProvider()).run_once()
    with app.state.sessions() as db:
        assert db.get(Job, saved["job_id"]).status == "succeeded"
        assert db.scalar(select(Note.id).where(Note.capture_id == saved["capture_id"])) is not None
        assert db.scalar(select(func.count()).select_from(Reminder)) == 0


def test_long_task_text_fits_a_reminder(reminders):
    app, client, ids = reminders
    with app.state.sessions.begin() as db:
        item = db.get(Item, ids["item"])
        item.due_text, item.text = "завтра", "Очень длинная задача " * 100
    assert len(run_auto(app, ids["note"])) == 1
    (row,) = auto_rows(app)
    assert 0 < len(row.text) <= 1000


# Message text

def test_reminder_message_is_plain_text_with_the_note_title():
    reminder = SimpleNamespace(text="  Позвонить маме  ")
    assert reminder_message_text(reminder, SimpleNamespace(title="План недели")) == (
        "Напоминание\n\nПозвонить маме\n\nИз заметки «План недели»"
    )
    assert reminder_message_text(reminder, SimpleNamespace(title="  ")) == "Напоминание\n\nПозвонить маме"


def test_reminder_message_stays_within_the_telegram_limit():
    result = reminder_message_text(SimpleNamespace(text="😀" * 1000), SimpleNamespace(title="я" * 200))
    assert len(result.encode("utf-16-le")) // 2 <= 4096
    assert result.startswith("Напоминание\n\n😀") and result.endswith("»")
    result = reminder_message_text(SimpleNamespace(text="😀" * 5000), SimpleNamespace(title="Заметка " * 100))
    assert len(result.encode("utf-16-le")) // 2 <= 4096
    assert result.endswith("»") and "…" in result


def test_clip_never_splits_a_character():
    assert clip("abc", 3) == "abc"
    assert clip("abcdef", 4) == "abc…"
    cut = clip("😀" * 10, 6)
    assert cut == "😀😀…" and len(cut.encode("utf-16-le")) // 2 <= 6
    assert clip("😀" * 10, 4) == "😀…"
    assert clip("😀", 2) == "😀"


def test_due_reminder_reaches_telegram_with_text_note_and_link(reminders):
    app, client, ids = reminders
    processed(app, client)
    (row,) = auto_rows(app)
    claim(client)  # The processing reply goes first.
    with app.state.sessions.begin() as db:
        db.get(Reminder, row.id).scheduled_at = time.time() - 1
        for delivery in db.scalars(select(Outbox).where(Outbox.job_id.is_not(None))):
            delivery.status = "sent"
    assert Scheduler(app.state.sessions).run_once() == 1
    sent = claim(client).json()["items"][0]
    assert sent["text"].startswith("Напоминание\n\nАндрей завтра уточнит ситуацию.\n\nИз заметки «")
    assert sent["note_url"].endswith("&reminder=" + row.id) and sent["callback_token"] is not None


# Buttons

def press(client, kind, update_id=3, **extra):
    return action(client, update_id, action=kind, **extra)


def extra_reminder(app, ids):
    with app.state.sessions.begin() as db:
        extra = Reminder(
            user_id=db.get(Reminder, ids["reminder"]).user_id, note_id=ids["note"], item_id=ids["item"],
            scheduled_at=time.time() + 7200, timezone="Europe/Moscow", text="Позвонить",
        )
        db.add(extra)
        db.flush()
        return extra.id


def test_snooze_for_an_hour_moves_the_same_reminder(task):
    app, client, ids = task
    before = time.time()
    response = press(client, "snooze_hour")
    assert response.status_code == 200, response.text
    with app.state.sessions() as db:
        reminder = db.get(Reminder, ids["reminder"])
        assert reminder.generation == 2 and reminder.status == "confirmed"
        assert before + 3590 <= reminder.scheduled_at <= time.time() + 3610
        expected = datetime.fromtimestamp(reminder.scheduled_at, MOSCOW).strftime("%H:%M")
        assert response.json() == {"status": "snoozed", "local_time": expected}
        assert db.get(Item, ids["item"]).status == "open"
        assert db.get(Outbox, ids["delivery"]).status == "sent"  # The delivered message stays as it was.
    assert press(client, "snooze_hour").json() == response.json()  # The same update is replayed.
    # The scheduler hands it over again when it is due, with a new generation and a new token.
    with app.state.sessions.begin() as db:
        db.get(Reminder, ids["reminder"]).scheduled_at = time.time() - 1
    assert Scheduler(app.state.sessions).run_once() == 1
    again = client.post(
        "/internal/v1/deliveries/claim", headers=HEADERS, json={"bot_id": BASE["bot_id"], "limit": 5},
    ).json()["items"]
    assert len(again) == 1 and again[0]["generation"] == 2 and again[0]["callback_token"] is not None


def test_snooze_until_tomorrow_keeps_the_local_time(task):
    app, client, ids = task
    with app.state.sessions() as db:
        original = datetime.fromtimestamp(db.get(Reminder, ids["reminder"]).scheduled_at, MOSCOW)
    response = press(client, "snooze_tomorrow")
    assert response.status_code == 200, response.text
    with app.state.sessions() as db:
        reminder = db.get(Reminder, ids["reminder"])
        moved = datetime.fromtimestamp(reminder.scheduled_at, MOSCOW)
        assert reminder.generation == 2 and reminder.status == "confirmed"
        assert moved.date() == datetime.now(MOSCOW).date() + timedelta(days=1)
        assert (moved.hour, moved.minute) == (original.hour, original.minute)
        assert response.json() == {"status": "snoozed", "local_time": f"{moved:%H:%M}"}


def test_tomorrow_is_the_same_wall_clock_time_across_dst():
    def row(local, zone):
        return SimpleNamespace(scheduled_at=local.replace(tzinfo=zone).timestamp(), timezone=zone.key)

    def at(year, month, day, hour, minute, zone):
        return datetime(year, month, day, hour, minute, tzinfo=zone)

    # Autumn: the night between 24 and 25 October 2026 is 25 hours long in Berlin.
    start = row(datetime(2026, 10, 24, 9, 0), BERLIN)
    moved = snooze_time(start, "snooze_tomorrow", at(2026, 10, 24, 10, 0, BERLIN).timestamp())
    assert datetime.fromtimestamp(moved, BERLIN) == at(2026, 10, 25, 9, 0, BERLIN)
    assert moved - start.scheduled_at == 25 * 3600
    # Spring: the night between 28 and 29 March 2026 is 23 hours long.
    start = row(datetime(2026, 3, 28, 9, 0), BERLIN)
    moved = snooze_time(start, "snooze_tomorrow", at(2026, 3, 28, 10, 0, BERLIN).timestamp())
    assert datetime.fromtimestamp(moved, BERLIN) == at(2026, 3, 29, 9, 0, BERLIN)
    assert moved - start.scheduled_at == 23 * 3600
    # A wall time that does not exist on that day moves forward with the clock, never into the past.
    gap = row(datetime(2026, 3, 28, 2, 30), BERLIN)
    moved = snooze_time(gap, "snooze_tomorrow", at(2026, 3, 28, 10, 0, BERLIN).timestamp())
    assert datetime.fromtimestamp(moved, BERLIN) == at(2026, 3, 29, 3, 30, BERLIN)
    # Tomorrow means the day after today, not the day after the schedule.
    late = row(datetime(2026, 10, 1, 9, 0), MOSCOW)
    moved = snooze_time(late, "snooze_tomorrow", at(2026, 10, 10, 23, 59, MOSCOW).timestamp())
    assert datetime.fromtimestamp(moved, MOSCOW) == at(2026, 10, 11, 9, 0, MOSCOW)
    # An hour is an hour.
    assert snooze_time(late, "snooze_hour", 1000.0) == 4600.0


def test_second_press_after_a_snooze_is_refused(task):
    app, client, ids = task
    assert press(client, "snooze_hour").status_code == 200
    for update_id, kind in ((4, "snooze_hour"), (5, "snooze_tomorrow"), (6, "done")):
        refused = press(client, kind, update_id)
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "action_expired"
    with app.state.sessions() as db:
        assert db.get(Reminder, ids["reminder"]).generation == 2
        assert db.get(Item, ids["item"]).status == "open"


@pytest.mark.parametrize("kind", ["done", "snooze_hour", "snooze_tomorrow"])
@pytest.mark.parametrize("change", ["generation", "cancelled", "pending", "item_idea"])
def test_no_button_works_on_a_stale_reminder(task, kind, change):
    app, client, ids = task
    with app.state.sessions() as db:
        reminder, delivery = db.get(Reminder, ids["reminder"]), db.get(Outbox, ids["delivery"])
        if change == "generation":
            reminder.generation += 1
        elif change == "cancelled":
            reminder.status = "cancelled"
        elif change == "pending":
            delivery.status, delivery.authorized_at = "pending", None
        else:
            db.get(Item, ids["item"]).kind = "idea"
        db.commit()
        before = (reminder.scheduled_at, reminder.generation, reminder.status)
    response = press(client, kind)
    assert response.status_code == 409 and response.json()["error"]["code"] == "action_expired"
    with app.state.sessions() as db:
        reminder = db.get(Reminder, ids["reminder"])
        assert (reminder.scheduled_at, reminder.generation, reminder.status) == before
        assert db.get(Item, ids["item"]).status == "open"


@pytest.mark.parametrize("kind", ["snooze_hour", "snooze_tomorrow"])
def test_snooze_is_refused_after_the_task_is_done_on_the_site(task, kind):
    app, client, ids = task
    with app.state.sessions() as db:
        db.get(Item, ids["item"]).status = "completed"
        db.commit()
    refused = press(client, kind)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "action_expired"
    assert press(client, "done", 4).json() == {"status": "already_completed"}
    with app.state.sessions() as db:
        assert db.get(Reminder, ids["reminder"]).generation == 1


@pytest.mark.parametrize("kind", ["done", "snooze_hour", "snooze_tomorrow"])
def test_a_stranger_cannot_press_the_buttons(task, kind):
    app, client, ids = task
    with TestClient(app) as other:
        user = register(other, "another")
        with app.state.sessions() as db:
            db.add(TelegramIdentity(user_id=user["id"], bot_id=BASE["bot_id"], telegram_user_id=17, chat_id=17))
            db.commit()
        response = action(other, 3, telegram_user_id=17, action=kind)
        assert response.status_code == 403 and response.json()["error"]["code"] == "action_forbidden"
    with app.state.sessions() as db:
        reminder = db.get(Reminder, ids["reminder"])
        assert reminder.generation == 1 and reminder.status == "confirmed"
        assert db.get(Item, ids["item"]).status == "open"


def test_unknown_action_is_a_validation_error(task):
    _, client, _ = task
    assert press(client, "delete").status_code == 422
    assert press(client, "").status_code == 422


def test_old_bare_token_still_means_done(task):
    app, client, ids = task
    response = action(client)  # No action field, as before the update.
    assert response.status_code == 200 and response.json() == {"status": "completed"}
    with app.state.sessions() as db:
        assert db.get(Item, ids["item"]).status == "completed"


def test_done_completes_the_task_like_the_site_and_is_idempotent(task):
    app, client, ids = task
    other = extra_reminder(app, ids)
    assert press(client, "done").json() == {"status": "completed"}
    assert press(client, "done", 4).json() == {"status": "already_completed"}
    with app.state.sessions() as db:
        item = db.get(Item, ids["item"])
        assert item.status == "completed" and item.version == 2
        assert db.get(Note, ids["note"]).version == 2
        # Every reminder of the task is closed, not only the one that was pressed.
        assert db.get(Reminder, other).status == "cancelled"
        assert db.get(Reminder, ids["reminder"]).status == "cancelled"


def test_done_on_a_note_reminder_marks_the_reminder_completed(task):
    app, client, ids = task
    with app.state.sessions.begin() as db:
        db.get(Reminder, ids["reminder"]).item_id = None
    assert press(client, "done").json() == {"status": "completed"}
    assert press(client, "done", 4).json() == {"status": "already_completed"}
    with app.state.sessions() as db:
        reminder = db.get(Reminder, ids["reminder"])
        assert reminder.status == "completed" and reminder.generation == 1
        assert db.get(Item, ids["item"]).status == "open"  # Nothing was tied to the task.
        assert db.get(Note, ids["note"]).version == 1
    # A completed reminder is not moved, and the site lists it with that status.
    refused = press(client, "snooze_hour", 5)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "action_expired"
    listed = client.get(f"/api/v1/notes/{ids['note']}/reminders").json()
    assert [r["status"] for r in listed] == ["completed"]
    assert Scheduler(app.state.sessions).run_once() == 0


@pytest.mark.parametrize("kind", ["snooze_hour", "snooze_tomorrow"])
def test_snooze_on_a_note_reminder(task, kind):
    app, client, ids = task
    with app.state.sessions.begin() as db:
        db.get(Reminder, ids["reminder"]).item_id = None
    response = press(client, kind)
    assert response.status_code == 200 and response.json()["status"] == "snoozed"
    with app.state.sessions() as db:
        assert db.get(Reminder, ids["reminder"]).generation == 2
        assert db.get(Item, ids["item"]).status == "open"


def test_site_shows_the_new_time_after_a_snooze(task):
    app, client, ids = task
    press(client, "snooze_hour")
    listed = client.get(f"/api/v1/notes/{ids['note']}/reminders").json()
    assert listed[0]["generation"] == 2 and listed[0]["status"] == "confirmed"
    assert datetime.fromisoformat(listed[0]["scheduled_at"]).timestamp() > time.time() + 3000
