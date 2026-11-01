"""Ранние напоминания «Через час: ...» для тех, кто просит напоминать дважды, и тихие часы."""

import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.deliveries import link_urls
from app.models import Note, Outbox, Reminder, UserProfile
from app.reminders import advance_moment, request_reminders, sync_advance
from app.scheduler import Scheduler
from tests.test_reminders import add, claim, payload
from tests.test_reminders import reminders as reminders  # noqa: F401

MOSCOW = ZoneInfo("Europe/Moscow")
QUIET = {"quiet": ((2, 0), (10, 0)), "dayparts": {"evening": (20, 0)}}
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=MOSCOW).timestamp()


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=MOSCOW)


def moment(main, prefs=None, now=NOW):
    result = advance_moment(main.timestamp(), "Europe/Moscow", prefs if prefs is not None else {}, now)
    return None if result is None else datetime.fromtimestamp(result, MOSCOW)


# ---- Время ----


def test_the_advance_reminder_is_one_hour_earlier():
    assert moment(at(15, 15, 30)) == at(15, 14, 30)
    assert moment(at(15, 15), QUIET) == at(15, 14)


def test_no_advance_when_the_hour_is_already_gone():
    assert moment(at(14, 12, 30)) is None
    assert moment(at(14, 13, 0)) is None  # exactly an hour: the moment is now
    assert moment(at(14, 13, 1)) is not None


def test_inside_quiet_hours_it_moves_to_the_end_of_them_if_before_the_main_time():
    assert moment(at(15, 10, 30), QUIET) == at(15, 10, 0)
    assert moment(at(15, 10, 59), QUIET) == at(15, 10, 0)


def test_otherwise_it_goes_to_the_evening_of_the_day_before():
    assert moment(at(15, 10, 0), QUIET) == at(14, 20, 0)  # the quiet end is the main time itself
    assert moment(at(15, 3, 0), QUIET) == at(14, 20, 0)
    assert moment(at(16, 9, 0), {**QUIET, "dayparts": {"evening": (21, 30)}}) == at(15, 21, 30)
    assert moment(at(16, 9, 0), {"quiet": ((2, 0), (10, 0))}) == at(15, 20, 0)  # default evening


def test_if_nothing_fits_there_is_no_advance_reminder():
    assert moment(at(14, 13, 30), {"quiet": ((12, 0), (14, 0))}) is None  # evening of the day before is the past
    assert moment(at(15, 10, 0), {"quiet": ((2, 0), (10, 0)), "dayparts": {"evening": (3, 0)}}) is None  # evening in quiet


def test_quiet_hours_over_midnight():
    quiet = {"quiet": ((23, 0), (7, 0))}
    assert moment(at(15, 7, 30), quiet) == at(15, 7, 0)
    assert moment(at(15, 0, 30), quiet) == at(14, 20, 0)  # advance 23:30 would end at 07:00 after the main time
    assert moment(at(15, 23, 30), quiet) == at(15, 22, 30)


# ---- Связь с главным напоминанием ----


def make_twice(app, user_id, quiet=None):
    with app.state.sessions.begin() as db:
        db.merge(UserProfile(
            user_id=user_id, state="ready", answers={"q1": "x"}, facts=[], updated_at=time.time(),
            prefs={"dayparts": {}, "quiet_hours": quiet, "wake": None, "sleep": None, "reminder_style": "twice",
                   "note_style": None, "vocabulary": [], "suggested_categories": []},
        ))


def advance_rows(app):
    with app.state.sessions() as db:
        rows = db.scalars(select(Reminder).where(Reminder.advance_of.is_not(None)).order_by(Reminder.id)).all()
        for row in rows:
            db.expunge(row)
        return rows


def body_at(offset, ids, **extra):
    when = datetime.fromtimestamp(time.time() + offset, UTC).isoformat()
    return {**payload(ids["item"]), "scheduled_at": when, **extra}


def test_a_person_who_wants_one_reminder_gets_no_advance(reminders):
    app, client, ids = reminders
    assert add(client, ids, body_at(4 * 3600, ids)).status_code == 201
    assert advance_rows(app) == []


def test_a_web_reminder_gets_its_advance_twin(reminders):
    app, client, ids = reminders
    make_twice(app, ids["user"])
    main = add(client, ids, body_at(4 * 3600, ids)).json()
    (twin,) = advance_rows(app)
    assert twin.advance_of == main["id"] and twin.text == "Через час: Позвонить" and twin.status == "confirmed"
    assert abs(twin.scheduled_at - (datetime.fromisoformat(main["scheduled_at"]).timestamp() - 3600)) < 1
    assert (twin.note_id, twin.item_id, twin.timezone) == (ids["note"], ids["item"], "Europe/Moscow")
    listed = client.get("/api/v1/notes/" + ids["note"] + "/reminders").json()
    assert {row["id"]: row["advance_of"] for row in listed} == {main["id"]: None, twin.id: main["id"]}
    assert all(row["calendar_url"] is None for row in listed if row["advance_of"])  # No calendar link for the twin
    assert [row["calendar_url"] is not None for row in listed if not row["advance_of"]] == [True]


def test_replaying_the_creation_makes_one_twin(reminders):
    app, client, ids = reminders
    make_twice(app, ids["user"])
    body = body_at(4 * 3600, ids)
    assert add(client, ids, body, "same").status_code == 201
    assert add(client, ids, body, "same").status_code == 201
    assert len(advance_rows(app)) == 1


def test_moving_the_main_reminder_moves_the_twin(reminders):
    app, client, ids = reminders
    make_twice(app, ids["user"])
    main = add(client, ids, body_at(4 * 3600, ids)).json()
    (before,) = advance_rows(app)
    edit = {k: v for k, v in body_at(9 * 3600, ids).items() if k != "item_id"}
    moved = client.patch("/api/v1/reminders/" + main["id"], json={**edit, "generation": 1})
    assert moved.status_code == 200
    (twin,) = advance_rows(app)
    assert twin.id == before.id and twin.generation == before.generation + 1
    assert abs(twin.scheduled_at - (datetime.fromisoformat(moved.json()["scheduled_at"]).timestamp() - 3600)) < 1


def test_cancelling_the_main_reminder_cancels_the_twin(reminders):
    app, client, ids = reminders
    make_twice(app, ids["user"])
    main = add(client, ids, body_at(4 * 3600, ids)).json()
    assert client.post("/api/v1/reminders/" + main["id"] + "/cancel", json={"generation": 1}).status_code == 200
    (twin,) = advance_rows(app)
    assert twin.status == "cancelled"


def test_marking_the_task_done_cancels_both(reminders):
    app, client, ids = reminders
    make_twice(app, ids["user"])
    main = add(client, ids, body_at(4 * 3600, ids)).json()
    note = client.get("/api/v1/notes/" + ids["note"]).json()
    done = client.patch(
        f"/api/v1/notes/{ids['note']}/items/{ids['item']}",
        json={"version": note["version"], "kind": "task", "text": "Позвонить", "status": "completed"},
    )
    assert done.status_code == 200, done.text
    with app.state.sessions() as db:
        assert {r.status for r in db.scalars(select(Reminder))} == {"cancelled"}
    assert main["id"]


def test_done_on_a_reminder_without_a_task_finishes_the_pair(reminders):
    app, client, ids = reminders
    make_twice(app, ids["user"])
    main = add(client, ids, {**body_at(4 * 3600, ids), "item_id": None}).json()
    (twin,) = advance_rows(app)
    from app.reminders import complete_reminder

    with app.state.sessions.begin() as db:
        complete_reminder(db, db.get(Reminder, twin.id))  # «Выполнено» on the advance message
    with app.state.sessions() as db:
        assert db.get(Reminder, twin.id).status == "completed" and db.get(Reminder, main["id"]).status == "completed"
    main2 = add(client, ids, {**body_at(6 * 3600, ids), "item_id": None, "text": "Другое"}, "k2").json()
    with app.state.sessions.begin() as db:
        complete_reminder(db, db.get(Reminder, main2["id"]))
    assert {r.status for r in advance_rows(app) if r.advance_of == main2["id"]} == {"cancelled"}


def test_switching_off_twice_stops_new_twins_and_a_move_cancels_the_old(reminders):
    app, client, ids = reminders
    make_twice(app, ids["user"])
    main = add(client, ids, body_at(4 * 3600, ids)).json()
    with app.state.sessions.begin() as db:
        db.get(UserProfile, ids["user"]).prefs = {**db.get(UserProfile, ids["user"]).prefs, "reminder_style": "once"}
    edit = {k: v for k, v in body_at(5 * 3600, ids).items() if k != "item_id"}
    client.patch("/api/v1/reminders/" + main["id"], json={**edit, "generation": 1})
    assert [r.status for r in advance_rows(app)] == ["cancelled"]


def test_a_main_reminder_within_the_hour_has_no_twin(reminders):
    app, client, ids = reminders
    make_twice(app, ids["user"])
    add(client, ids, body_at(1800, ids))
    assert advance_rows(app) == []


def test_ai_made_reminders_get_twins_and_quiet_hours_move_only_the_twin(reminders):
    app, client, ids = reminders
    make_twice(app, ids["user"], quiet={"from": "02:00", "to": "10:00"})
    now = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)  # 12:00 in Moscow
    with app.state.sessions.begin() as db:
        created = request_reminders(
            db, db.get(Note, ids["note"]), "Напомни завтра в 10:30 позвонить маме", app.state.settings, now=now,
        )
        (main,) = created
    assert datetime.fromtimestamp(main.scheduled_at, MOSCOW) == datetime(2026, 10, 11, 10, 30, tzinfo=MOSCOW)
    (twin,) = advance_rows(app)
    assert datetime.fromtimestamp(twin.scheduled_at, MOSCOW) == datetime(2026, 10, 11, 10, 0, tzinfo=MOSCOW)
    assert twin.text == "Через час: Позвонить маме"


def test_an_explicit_time_in_quiet_hours_is_kept(reminders):
    app, client, ids = reminders
    make_twice(app, ids["user"], quiet={"from": "02:00", "to": "10:00"})
    now = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    with app.state.sessions.begin() as db:
        (main,) = request_reminders(
            db, db.get(Note, ids["note"]), "Напомни завтра в 3 часа ночи позвонить маме", app.state.settings, now=now,
        )
    assert datetime.fromtimestamp(main.scheduled_at, MOSCOW) == datetime(2026, 10, 11, 3, 0, tzinfo=MOSCOW)
    (twin,) = advance_rows(app)  # The twin is beresta's choice, so it moves: the evening of the day before
    assert datetime.fromtimestamp(twin.scheduled_at, MOSCOW) == datetime(2026, 10, 10, 20, 0, tzinfo=MOSCOW)


def test_the_twin_is_delivered_like_any_reminder_without_a_calendar_link(reminders):
    app, client, ids = reminders
    make_twice(app, ids["user"])
    main = add(client, ids, {**body_at(4 * 3600, ids), "item_id": None}).json()
    (twin,) = advance_rows(app)
    with app.state.sessions.begin() as db:
        db.get(Reminder, twin.id).scheduled_at = time.time() - 1
    assert Scheduler(app.state.sessions).run_once() == 1
    (item,) = claim(client).json()["items"]
    assert item["text"].startswith("Напоминание\n\nЧерез час: Позвонить") and item["links"] == []
    with app.state.sessions() as db:
        assert db.scalar(select(Outbox.reminder_id)) == twin.id
        assert link_urls(db, ids["user"], [twin.id, main["id"]], app.state.settings)[0] is None
        assert link_urls(db, ids["user"], [twin.id, main["id"]], app.state.settings)[1] is not None


def test_sync_is_safe_for_a_twin_and_a_missing_profile(reminders):
    app, client, ids = reminders
    main = add(client, ids, body_at(4 * 3600, ids)).json()
    with app.state.sessions.begin() as db:
        assert sync_advance(db, db.get(Reminder, main["id"])) is None
    assert advance_rows(app) == []
