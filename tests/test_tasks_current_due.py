"""The «Все задачи» list shows the current time of the task's reminder, not the time quoted in the note."""

import time
from datetime import UTC, datetime

from sqlalchemy import select

from app.models import Item, Reminder
from app.reminders import reschedule, snooze_time
from tests.test_reminders import add, payload, reminders  # noqa: F401  (fixture)


def task_due(client, item_id, status="open"):
    rows = client.get("/api/v1/tasks", params={"status": status}).json()["items"]
    row = next(r for r in rows if r["id"] == item_id)
    return row["due_resolved_at"], row["due_has_time"]




def overdue_reminder(app, client, ids):
    created = add(client, ids)
    assert created.status_code == 201, created.text
    reminder_id = created.json()["id"]
    with app.state.sessions() as db:
        db.get(Reminder, reminder_id).scheduled_at = time.time() - 7200
        item = db.get(Item, ids["item"])
        item.due_at = time.time() - 86400  # the old date on the task, which must not win over the reminder
        db.commit()
    return reminder_id


def test_overdue_task_shows_old_time_then_new_after_web_reschedule(reminders):  # noqa: F811
    app, client, ids = reminders
    reminder_id = overdue_reminder(app, client, ids)
    due, has_time = task_due(client, ids["item"])
    assert has_time and due < time.time()
    when = datetime.fromtimestamp(time.time() + 5 * 3600, UTC).isoformat()
    edit = {k: v for k, v in payload().items() if k != "item_id"}
    assert client.patch("/api/v1/reminders/" + reminder_id, json={**edit, "scheduled_at": when, "generation": 1}).status_code == 200
    due, has_time = task_due(client, ids["item"])
    assert has_time and abs(due - datetime.fromisoformat(when).timestamp()) < 1


def test_bot_snooze_and_time_change_move_the_task_date(reminders):  # noqa: F811
    app, client, ids = reminders
    reminder_id = overdue_reminder(app, client, ids)
    now = time.time()
    with app.state.sessions() as db:
        row = db.get(Reminder, reminder_id)
        row.status = "sent"  # the bot delivered it, the person did nothing
        db.commit()
        assert task_due(client, ids["item"])[0] < now
        when = snooze_time(row, "snooze_hour", now)
        reschedule(db, row, when, row.timezone, row.text, "telegram")
        db.commit()
    assert abs(task_due(client, ids["item"])[0] - (now + 3600)) < 5
    with app.state.sessions() as db:
        row = db.get(Reminder, reminder_id)
        tomorrow = snooze_time(row, "snooze_tomorrow", now)
        reschedule(db, row, tomorrow, row.timezone, row.text, "telegram")
        db.commit()
    assert abs(task_due(client, ids["item"])[0] - tomorrow) < 5


def test_cancelled_or_finished_reminder_does_not_decide_the_date(reminders):  # noqa: F811
    app, client, ids = reminders
    reminder_id = overdue_reminder(app, client, ids)
    with app.state.sessions() as db:
        db.get(Reminder, reminder_id).status = "cancelled"
        db.commit()
    due, _ = task_due(client, ids["item"])
    with app.state.sessions() as db:
        item = db.scalar(select(Item).where(Item.id == ids["item"]))
        assert item.due_at is not None
    # Without a live reminder the date of the task itself decides again.
    assert due < time.time() - 80000
